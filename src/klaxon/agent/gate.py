"""The approval gate: a write action runs only with an approval on file, which `make approve` writes when the
request's id is typed at a terminal (Aravind's choice, 2026-10-19).

For a run whose diagnosis names an action (fix_action, klaxon.agent.contract):

    1. Code plans the action from the live state (klaxon.agent.actions). A plan that cannot be made is refused,
       and nobody is asked.
    2. The run saves an approval request in its folder (approval-request.json), says how to answer it, and waits.
    3. In his own terminal, Aravind runs `make approve RUN=<folder>`. It shows the request, and he types the
       request's id to approve it; anything else denies it. The command refuses unless it can read from a
       terminal, so a process without one (Claude's shell, for one) cannot approve.
    4. No decision within 10 minutes means denied (by the wall clock too: a Mac's other clock stops while it
       sleeps). A decision counts only for the request it names, whole: one that names another request, or this
       one changed after it was saved, is denied. A run that stops while it waits closes its request with a denial.
    5. An approved plan runs, and only if what it was made on still holds (klaxon.agent.actions.run).
    6. After it, code verifies (klaxon.agent.verify). A failed verification proposes undoing the action, through
       this gate (propose_undo); in this system every undo is a person's call, so nobody is asked.

The decision file can be written once. Every step goes to the folder's log (gate.jsonl): the request; each
decision `make approve` writes; the decision the run takes, with the decision file's own SHA-256; what ran and
what came of it. With times, and never the Mac's user name. `make approve` logs the SHA-256 of the decision it
wrote, and the run the SHA-256 of the one it took: a decision the run took that no line of `make approve` names
was not written by `make approve`, unless the log too was written by hand, or `make approve` was stopped between
writing the decision and its line.

Its limit, said plainly: any process running as Aravind can approve. It can write the decision file, run
`make approve` in a pseudo-terminal it makes (the tests do), type into his terminal (tools that drive a terminal
can, Claude's among them), change this code, or write the log. That Claude never approves is a rule; the log is a
record for after the fact, not a lock.

Run: python -m klaxon.agent.gate approve FOLDER     (make approve RUN=FOLDER)
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from klaxon.agent import actions
from klaxon.agent.actions import ActionRefused, Plan
from klaxon.agent.contract import Diagnosis
from klaxon.agent.tools import clean, clean_text
from klaxon.patient.deploys import Deployer, DeployError
from klaxon.patient.stack import StackError

REQUEST, DECISION, LOG = "approval-request.json", "approval-decision.json", "gate.jsonl"
WAIT_SECONDS = 600.0  # 10 minutes: no decision by then means denied
POLL_SECONDS = 1.0
REQUEST_ID = re.compile(r"r-[0-9a-f]{4}")
REQUEST_KEYS = {"label", "id", "created_at", "expires_at", "run", "alert", "action", "plan", "diagnosis", "sha256"}
DECISION_KEYS = {"label", "request_id", "request_sha256", "decision", "at", "how"}
REQUEST_LABEL = "An approval request from Klaxon: a write action that runs only if a person approves it."
DECISION_LABEL = "A decision on one approval request."
TYPED = "typed at a terminal"
BY = "Klaxon, approved request"  # how the deploy log names a deploy the gate ran, before the request's id


class GateError(RuntimeError):
    """A request or a decision that cannot be used."""


@dataclass(frozen=True)
class Decision:
    decision: str  # approved, denied or expired
    how: str
    at: str


@dataclass(frozen=True)
class Outcome:
    """What came of a diagnosis at the gate."""

    result: str  # no action, refused, denied, expired, not run, failed, done
    detail: str
    request_id: str | None = None
    deploy: dict | None = None


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _seconds(text: object) -> float:
    """A UTC time this gate wrote, back to seconds; GateError for anything else."""
    if not isinstance(text, str) or not text.endswith("Z"):
        raise GateError("a time that is not one this gate writes")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00").timestamp()
    except ValueError:
        raise GateError("a time that is not one this gate writes") from None


def request_sha256(request: Mapping) -> str:
    """The SHA-256 of a request without its own sha256 field, as compact JSON with sorted keys."""
    body = {key: value for key, value in request.items() if key != "sha256"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


# Files: a request and a decision are each written once, whole; the log only grows.


def _create_once(path: Path, value: Mapping) -> bytes | None:
    """Write path whole, only if it does not exist yet: the bytes written when this call made it, else None. A
    reader never sees half a file, and of two writers exactly one wins (a hard link fails if the name is taken)."""
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_bytes(data)
    try:
        os.link(temp, path)
        return data
    except FileExistsError:
        return None
    finally:
        temp.unlink(missing_ok=True)


def log(folder: Path, event: str, now: Callable[[], float] = time.time, **fields: object) -> None:
    """One line in the folder's gate log, under an exclusive lock: the run and `make approve` both write to it."""
    record = {"at": utc(now()), "event": event} | {key: clean(value) for key, value in fields.items()}
    with open(Path(folder) / LOG, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_request(folder: Path) -> dict:
    """The folder's request, checked whole: GateError when it is missing, cannot be read, or was changed."""
    try:
        value = json.loads((Path(folder) / REQUEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GateError("this folder holds no approval request") from None
    except (OSError, ValueError):
        raise GateError("the approval request cannot be read") from None
    if not isinstance(value, dict) or set(value) != REQUEST_KEYS:
        raise GateError("the file is not an approval request")
    if not isinstance(value["id"], str) or not REQUEST_ID.fullmatch(value["id"]):
        raise GateError("the approval request has no valid id")
    if value["sha256"] != request_sha256(value):
        raise GateError("the approval request was changed after the run saved it")
    _seconds(value["created_at"]), _seconds(value["expires_at"])
    if not _shaped(value):  # sealed again by someone, but not in the shape a run writes: refused, never shown
        raise GateError("the approval request is not in the shape a run writes")
    return value


def _shaped(request: Mapping) -> bool:
    """Whether a request has every field a run writes and render() shows, each of the kind it should be."""
    plan, found, action = request.get("plan"), request.get("diagnosis"), request.get("action")
    if not (isinstance(plan, dict) and isinstance(found, dict) and isinstance(action, dict)):
        return False
    if "confidence" not in found:
        return False
    texts = (plan.get("change"), plan.get("undo"), action.get("name"), found.get("hypothesis"))
    texts += (found.get("blast_radius"), found.get("rollback_plan"))
    if not all(isinstance(text, str) for text in texts) or not isinstance(action.get("arguments"), dict):
        return False
    undoes, fix, evidence = plan.get("undoes"), found.get("proposed_fix"), found.get("evidence")
    if not isinstance(undoes, dict) or not {"seq", "at", "by", "container"} <= set(undoes):
        return False
    if not isinstance(fix, dict) or not {"kind", "target", "summary"} <= set(fix):
        return False
    if not isinstance(evidence, list) or not all(
        isinstance(item, dict) and {"call_id", "tool", "shows"} <= set(item) for item in evidence
    ):
        return False
    return request.get("alert") is None or isinstance(request["alert"], dict)


def read_decision(folder: Path) -> dict | None:
    """The folder's decision as written, or None when there is none yet. GateError when it cannot be read."""
    raw = _decision_bytes(folder)
    return None if raw is None else _parse_decision(raw)


def _decision_bytes(folder: Path) -> bytes | None:
    try:
        return (Path(folder) / DECISION).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise GateError("the decision file cannot be read") from None


def _parse_decision(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise GateError("the decision file cannot be read") from None
    if not isinstance(value, dict) or set(value) != DECISION_KEYS:
        raise GateError("the decision file is not a decision")
    return value


# The run's side: make the request, save it, wait for the decision.


def make_request(
    plan: Plan,
    diagnosis: Diagnosis,
    folder: Path,
    alert: Mapping | None = None,
    now: Callable[[], float] = time.time,
    wait_seconds: float = WAIT_SECONDS,
) -> dict:
    """The request for this plan: what will change and what it undoes, worked out by code; and, apart, the
    diagnosis in the model's own words. Cleaned (hidden characters out, the home folder as ~), and sealed with its
    own SHA-256, which the decision must name."""
    made = now()
    found = diagnosis.as_dict()
    body = {
        "label": REQUEST_LABEL,
        "id": f"r-{secrets.token_hex(2)}",
        "created_at": utc(made),
        "expires_at": utc(made + wait_seconds),
        "run": Path(folder).name,
        "alert": None if alert is None else dict(alert),
        "action": {"name": plan.action, "arguments": plan.arguments},
        "plan": plan.as_dict(),
        "diagnosis": {
            "hypothesis": found["hypothesis"],
            "confidence": found["confidence"],
            "evidence": [{key: item[key] for key in ("call_id", "tool", "shows")} for item in found["evidence"]],
            "proposed_fix": {key: found["proposed_fix"][key] for key in ("kind", "target", "summary")},
            "blast_radius": found["blast_radius"],
            "rollback_plan": found["rollback_plan"],
        },
    }
    body = clean(body)
    body["sha256"] = request_sha256(body)
    return body


def judge(found: Mapping | None, request: Mapping) -> Decision:
    """What a decision file means for this request: approved only when it names this request, whole, and was made
    before the request expired. Anything else is a denial."""
    if found is None:
        return Decision("denied", "the decision file cannot be read", "")
    at = found.get("at", "")
    if found.get("request_id") != request["id"] or found.get("request_sha256") != request["sha256"]:
        return Decision("denied", "the decision names another request, or this one changed", str(at))
    try:
        late = _seconds(at) > _seconds(request["expires_at"])
    except GateError:
        return Decision("denied", "the decision has no time this gate writes", str(at))
    if found.get("decision") == "approved":
        if late:
            return Decision("denied", "approved after the request expired", at)
        return Decision("approved", str(found.get("how", "")), at)
    if found.get("decision") in ("denied", "expired"):
        return Decision(found["decision"], str(found.get("how", "")), at)
    return Decision("denied", "the decision is not one this gate knows", str(at))


def wait(
    folder: Path,
    request: Mapping,
    timeout: float = WAIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> Decision:
    """Wait for the decision on this request, at most timeout seconds, and never past the request's own expiry by
    the wall clock; then write "expired" (denied) and log it. Both, because a Mac's monotonic clock stands still
    while it sleeps: a request open until 10:13 was once still waited on at 18:46 (found by the gate demo)."""
    folder = Path(folder)
    deadline = clock() + timeout
    expires = _seconds(request["expires_at"])
    while True:
        taken = _take(folder, request, now)
        if taken is not None:
            return taken
        left = min(deadline - clock(), expires - now())
        if left <= 0:
            break
        sleep(min(POLL_SECONDS, left))
    expired = {
        "label": DECISION_LABEL,
        "request_id": request["id"],
        "request_sha256": request["sha256"],
        "decision": "expired",
        "at": utc(now()),
        "how": f"no decision within {timeout:g} s",
    }
    if _create_once(folder / DECISION, expired) is None:  # a decision landed at the last moment: that one counts
        return _take(folder, request, now) or judge(None, request)
    log(folder, "expired", now, request=request["id"], how=expired["how"])
    return Decision("expired", expired["how"], expired["at"])


def _take(folder: Path, request: Mapping, now: Callable[[], float]) -> Decision | None:
    """The decision on file, as the run takes it; None while there is none. Logged either way, with the file's own
    SHA-256. One the run cannot take as written (it cannot be read, names another request, or came late) counts as
    a denial."""
    try:  # read once: what is judged and what is hashed are the same bytes
        raw = _decision_bytes(folder)
    except GateError:
        raw = b""
    if raw is None:
        return None
    sha = hashlib.sha256(raw).hexdigest() if raw else None
    try:
        found = _parse_decision(raw)
    except GateError:
        found = {}
    decision = judge(found or None, request)
    if decision.decision != found.get("decision"):
        log(folder, "decision not taken", now, request=request["id"], how=decision.how, decision_sha256=sha)
    else:
        log(
            folder,
            "decision taken",
            now,
            request=request["id"],
            decision=decision.decision,
            decided_at=decision.at,
            how=decision.how,
            decision_sha256=sha,
        )
    return decision


def _say(text: str) -> None:
    print(text, flush=True)  # at once, even into a pipe: the run waits, and whoever watches must see the request now


def how_to_answer(folder: Path, request: Mapping) -> str:
    plan = request["plan"]
    return "\n".join(
        [
            f"Klaxon asks to act: request {request['id']}, open until {request['expires_at']}.",
            f"  {plan['change']}",
            f"To approve or deny it, in your own terminal: make approve RUN={clean_text(str(folder))}",
            "No answer by then means denied.",
        ]
    )


def act(
    folder: Path,
    diagnosis: Diagnosis,
    deployer: Deployer,
    alert: Mapping | None = None,
    timeout: float = WAIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    say: Callable[[str], None] = _say,
    before_asking: Callable[[], None] | None = None,
) -> Outcome:
    """The gate, for one diagnosis: plan its action, ask, wait, and run it only if a person approved it.
    before_asking: called once a plan is made, right before its request is (week 4, item 2: the run reads the
    money then, so the evidence has the damage at the request)."""
    folder = Path(folder)
    named = diagnosis.fix_action
    if named is None:
        log(folder, "no action", now, why="the diagnosis names no action")
        return Outcome("no action", "the diagnosis names no action")
    try:
        planned = actions.plan(named.name, named.arguments, deployer)
    except ActionRefused as exc:
        log(folder, "refused", now, action=named.name, arguments=named.arguments, why=str(exc))
        return Outcome("refused", str(exc))
    if before_asking is not None:
        before_asking()
    request = make_request(planned, diagnosis, folder, alert, now, timeout)
    if _create_once(folder / REQUEST, request) is None:
        raise GateError("this folder already holds an approval request: one request per run")
    try:  # from here, a run that stops (Ctrl-C, a stop signal, a failed write) closes its request: no later yes counts
        log(
            folder,
            "requested",
            now,
            request=request["id"],
            action=request["action"],
            change=planned.change,
            undo=planned.undo,
            expires_at=request["expires_at"],
        )
        say(how_to_answer(folder, request))
        decision = wait(folder, request, timeout, clock, sleep, now)
    except BaseException:
        _close(folder, request, now)
        raise
    if decision.decision != "approved":
        return Outcome(decision.decision, decision.how, request["id"])
    log(folder, "running", now, request=request["id"], change=planned.change)
    try:
        record = actions.run(planned, deployer, by=f"{BY} {request['id']}")
    except ActionRefused as exc:
        log(folder, "not run", now, request=request["id"], why=str(exc))
        return Outcome("not run", str(exc), request["id"])
    except (DeployError, StackError) as exc:
        why = f"{type(exc).__name__}: {clean_text(str(exc), 300)}"
        log(folder, "failed", now, request=request["id"], why=why)
        return Outcome("failed", why, request["id"])
    except BaseException as exc:  # anything else, an interrupt included: the log says so, then it goes on up
        log(folder, "failed", now, request=request["id"], why=f"{type(exc).__name__}: {clean_text(str(exc), 300)}")
        raise
    log(folder, "done", now, request=request["id"], deploy=asdict(record))
    return Outcome("done", planned.change, request["id"], asdict(record))


STOPPED = "the run stopped before it took a decision"


def _close(folder: Path, request: Mapping, now: Callable[[], float]) -> None:
    """Close the request of a run that stopped before it took a decision: a denial, written once, so `make approve`
    finds it decided and logs no approval for a run that is gone. A decision that landed first stands as written;
    the log then shows that no run took it."""
    closed = {
        "label": DECISION_LABEL,
        "request_id": request["id"],
        "request_sha256": request["sha256"],
        "decision": "denied",
        "at": utc(now()),
        "how": STOPPED,
    }
    if _create_once(Path(folder) / DECISION, closed) is not None:
        log(folder, "stopped", now, request=request["id"], how=STOPPED)


def decided(folder: Path) -> dict | None:
    """The decision the run took, as its log says, and whether `make approve` wrote it: a line of `make approve`
    names the same SHA-256. None when the run took none (no request, or no answer: an expiry is the run's own)."""
    try:
        text = (Path(folder) / LOG).read_text(encoding="utf-8")
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise GateError("the gate's log cannot be read") from None
    lines = [line for line in lines if isinstance(line, dict)]
    taken = [line for line in lines if line.get("event") == "decision taken"]
    if not taken:
        return None
    sha = taken[-1].get("decision_sha256")
    written = {line.get("decision_sha256") for line in lines if line.get("event") in ("approved", "denied")}
    return {
        "decision": taken[-1].get("decision"),
        "how": taken[-1].get("how"),
        "at": taken[-1].get("decided_at"),
        "written_by_make_approve": sha is not None and sha in written,
    }


PERSONS_CALL = "Undoing it is a person's call: Klaxon asks nobody to approve it."


def propose_undo(
    folder: Path, done: Outcome, deployer: Deployer, why: str, now: Callable[[], float] = time.time
) -> Outcome:
    """After a failed verification (klaxon.agent.verify): propose undoing a done action, through the gate. The undo
    is planned as any action is, by code, with the same rules (actions.plan_undo). In this system every undo of a
    rollback would deploy a known-bad setting, which Klaxon never does: so the proposal is refused, and says plainly
    that undoing it is a person's call. Nobody is asked, and nothing runs. An undo Klaxon could run would need an
    approval request of its own, saying why, which is not built: it is refused too."""
    folder = Path(folder)
    if done.result != "done" or done.deploy is None:
        raise GateError("only an action that ran can be undone")
    log(folder, "undo proposed", now, request=done.request_id, why=why)
    try:
        planned = actions.plan_undo(done.deploy, deployer)
    except ActionRefused as exc:
        log(folder, "undo refused", now, request=done.request_id, why=str(exc))
        return Outcome("refused", f"Klaxon will not undo it: {exc}. {PERSONS_CALL}", done.request_id)
    detail = (
        f"Klaxon will not undo it: undoing it would {planned.change}, and an approval request for an undo is not"
        f" built. {PERSONS_CALL}"
    )
    log(folder, "undo refused", now, request=done.request_id, why="an approval request for an undo is not built")
    return Outcome("refused", detail, done.request_id)


# The person's side: make approve.


class Terminal:
    """A person's terminal. The request is shown, and the answer read, through /dev/tty, the process's
    controlling terminal: never through a pipe. A process without one gets None from open()."""

    def __init__(self, reader: TextIO, writer: TextIO) -> None:
        self.reader, self.writer = reader, writer

    @staticmethod
    def open() -> Terminal | None:
        if not sys.stdin.isatty():
            return None
        # Two handles: a terminal cannot seek, so one opened to read and write as text fails ("not seekable").
        try:
            reader = open("/dev/tty", encoding="utf-8")  # kept open while the person is asked
        except OSError:
            return None
        try:
            return Terminal(reader, open("/dev/tty", "w", encoding="utf-8"))
        except OSError:
            reader.close()
            return None

    def write(self, text: str) -> None:
        self.writer.write(text)
        self.writer.flush()

    def ask(self, question: str) -> str:
        self.write(question)
        return self.reader.readline()  # "" at the end of input: no answer, so no approval


def _one_line(text: object, limit: int = 1000) -> str:
    """Text from the request, on one line: nothing in it can pass for a line of the request's own."""
    return " ".join(clean_text(str(text), limit).split())


def render(request: Mapping, now: float) -> str:
    """The request as Aravind reads it: the plan code worked out, then the model's diagnosis, apart and labeled."""
    left = _seconds(request["expires_at"]) - now
    plan, found, alert = request["plan"], request["diagnosis"], request.get("alert")
    undoes = plan["undoes"]
    lines = [
        f"Klaxon asks to act. Request {request['id']}, made {request['created_at']}; it expires at"
        f" {request['expires_at']} (in {int(left // 60)} min {int(left % 60)} s).",
        f"Run: {_one_line(request['run'], 200)}",
    ]
    if alert:
        lines.append(
            f"Alert (written by the alert rules): {_one_line(alert.get('name'), 100)}: {_one_line(alert.get('summary'))}"
        )
    lines += [
        "",
        "The action, planned by code from the stack as it was when the run asked:",
        f"  {_one_line(request['action']['name'], 64)} {_one_line(json.dumps(request['action']['arguments']), 300)}",
        f"  change:  {_one_line(plan['change'])}",
        f"  undoes:  deploy {undoes['seq']}, at {_one_line(undoes['at'], 40)}, by {_one_line(undoes['by'], 200)}"
        f" (container {_one_line(undoes['container'], 12)})",
        f"  undo:    {_one_line(plan['undo'])}",
        "",
        "The diagnosis, in the model's own words (it read untrusted tool output: check it):",
        f"  hypothesis:     {_one_line(found['hypothesis'])}",
        f"  confidence:     {_one_line(found['confidence'], 20)}",
    ]
    for item in found["evidence"]:
        lines.append(
            f"  evidence:       {_one_line(item['call_id'], 8)} {_one_line(item['tool'], 64)}: {_one_line(item['shows'])}"
        )
    fix = found["proposed_fix"]
    lines += [
        f"  its fix:        {_one_line(fix['kind'], 40)} {_one_line(fix['target'], 200)}: {_one_line(fix['summary'])}",
        f"  blast radius:   {_one_line(found['blast_radius'])}",
        f"  rollback plan:  {_one_line(found['rollback_plan'])}",
        "",
    ]
    return "\n".join(lines)


def approve(
    folder: Path,
    open_terminal: Callable[[], Terminal | None] = Terminal.open,
    now: Callable[[], float] = time.time,
) -> int:
    """make approve: show the folder's request in a terminal, and take the typed answer. 0 when a decision was
    written; 1 when there was nothing left to decide; 2 when it was refused."""
    folder = Path(folder).expanduser()
    terminal = open_terminal()
    if terminal is None:
        print(
            "Refused: make approve reads the answer from a terminal, and this process has none. Approvals are"
            " typed by a person.",
            file=sys.stderr,
        )
        if (folder / REQUEST).is_file():  # a run's folder: never a log anywhere else
            log(folder, "approval refused", now, why="make approve was run without a terminal")
        return 2
    try:
        request = read_request(folder)
        decided = read_decision(folder)
    except GateError as exc:
        print(f"Refused: {exc}.", file=sys.stderr)
        return 2
    if decided is not None:
        terminal.write(f"Already decided: {_one_line(decided.get('decision'), 20)}. Nothing to do.\n")
        return 1
    if now() >= _seconds(request["expires_at"]):
        terminal.write(f"The request expired at {request['expires_at']}: there is nothing to approve.\n")
        return 1
    terminal.write(render(request, now()))
    answer = terminal.ask(f"Type {request['id']} to approve it. Anything else denies it: ")
    if now() >= _seconds(request["expires_at"]):
        terminal.write("It expired while you read it: nothing was approved.\n")
        return 1
    approved = answer.strip() == request["id"]
    decision = {
        "label": DECISION_LABEL,
        "request_id": request["id"],
        "request_sha256": request["sha256"],
        "decision": "approved" if approved else "denied",
        "at": utc(now()),
        "how": TYPED if approved else f"not the request's id, {TYPED}",
    }
    written = _create_once(folder / DECISION, decision)
    if written is None:
        terminal.write("It was decided meanwhile (it may have expired): nothing was written.\n")
        return 1
    sha = hashlib.sha256(written).hexdigest()  # the bytes written: the run logs the same hash when it takes them
    log(folder, decision["decision"], now, request=request["id"], how=decision["how"], decision_sha256=sha)
    if approved:
        terminal.write("Approved. The run acts now, if the stack still is as it was planned on.\n")
    else:
        terminal.write("Denied. Nothing runs.\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m klaxon.agent.gate", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    answer = commands.add_parser("approve", help="show a run's approval request, and approve or deny it")
    answer.add_argument("folder", type=Path, help="the run's folder (make approve RUN=...)")
    args = parser.parse_args(argv)
    return approve(args.folder)


if __name__ == "__main__":
    raise SystemExit(main())
