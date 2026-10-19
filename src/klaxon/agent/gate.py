"""The approval gate: no write action runs without a person's typed yes (Aravind's choice, 2026-10-19).

For a run whose diagnosis names an action (fix_action, klaxon.agent.contract):

    1. Code plans the action from the live state (klaxon.agent.actions). A plan that cannot be made is refused,
       and nobody is asked.
    2. The run saves an approval request in its folder (approval-request.json), says how to answer it, and waits.
    3. In his own terminal, Aravind runs `make approve RUN=<folder>`. It shows the request, and he types the
       request's id to approve it; anything else denies it. The command refuses unless it can read from a
       terminal, so a process without one (Claude's shell, for one) cannot approve.
    4. No decision within 10 minutes means denied. A decision counts only for the request it names, whole: one
       that names another request, or this one changed after it was saved, is denied.
    5. An approved plan runs, and only if what it was made on still holds (klaxon.agent.actions.run).

The decision file can be written once. Every step goes to the folder's log (gate.jsonl): the request, the
decision, what ran and what came of it, with times, and never the Mac's user name.

Its limit, said plainly: the code cannot tell a person from a process that writes the decision file itself, or
that makes itself a terminal (the tests do, with a pseudo-terminal). That Claude never approves is a rule, and the
log shows every decision.

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


def _create_once(path: Path, value: Mapping) -> bool:
    """Write path whole, only if it does not exist yet. True when this call made it. A reader never sees half a
    file, and of two writers exactly one wins (a hard link fails if the name is taken)."""
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.link(temp, path)
        return True
    except FileExistsError:
        return False
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
    return value


def read_decision(folder: Path) -> dict | None:
    """The folder's decision as written, or None when there is none yet. GateError when it cannot be read."""
    try:
        value = json.loads((Path(folder) / DECISION).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
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
    """Wait for the decision on this request, at most timeout seconds; then write "expired" (denied) and log it."""
    folder = Path(folder)
    deadline = clock() + timeout
    while True:
        taken = _take(folder, request, now)
        if taken is not None:
            return taken
        left = deadline - clock()
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
    if not _create_once(folder / DECISION, expired):  # a decision landed at the last moment: that one counts
        return _take(folder, request, now) or judge(None, request)
    log(folder, "expired", now, request=request["id"], how=expired["how"])
    return Decision("expired", expired["how"], expired["at"])


def _take(folder: Path, request: Mapping, now: Callable[[], float]) -> Decision | None:
    """The decision on file, as the run takes it; None while there is none. One the run cannot take as written (it
    cannot be read, names another request, or came late) is logged here, and counts as a denial."""
    try:
        found = read_decision(folder)
    except GateError:
        found = {}
    if found is None:
        return None
    decision = judge(found or None, request)
    if decision.decision != found.get("decision"):
        log(folder, "decision not taken", now, request=request["id"], how=decision.how)
    return decision


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
    say: Callable[[str], None] = print,
) -> Outcome:
    """The gate, for one diagnosis: plan its action, ask, wait, and run it only if a person approved it."""
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
    request = make_request(planned, diagnosis, folder, alert, now, timeout)
    if not _create_once(folder / REQUEST, request):
        raise GateError("this folder already holds an approval request: one request per run")
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
    if decision.decision != "approved":
        return Outcome(decision.decision, decision.how, request["id"])
    log(folder, "running", now, request=request["id"], change=planned.change)
    try:
        record = actions.run(planned, deployer, by=f"Klaxon, approved request {request['id']}")
    except ActionRefused as exc:
        log(folder, "not run", now, request=request["id"], why=str(exc))
        return Outcome("not run", str(exc), request["id"])
    except (DeployError, StackError) as exc:
        why = f"{type(exc).__name__}: {clean_text(str(exc), 300)}"
        log(folder, "failed", now, request=request["id"], why=why)
        return Outcome("failed", why, request["id"])
    log(folder, "done", now, request=request["id"], deploy=asdict(record))
    return Outcome("done", planned.change, request["id"], asdict(record))


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
        "The action, planned by code from the stack as it is now:",
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
        if folder.is_dir():
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
    if not _create_once(folder / DECISION, decision):
        terminal.write("It was decided meanwhile (it may have expired): nothing was written.\n")
        return 1
    log(folder, decision["decision"], now, request=request["id"], how=decision["how"])
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
