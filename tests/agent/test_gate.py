"""The approval gate, offline: the request a run saves, `make approve`, the waiter's rules, and act() from a
diagnosis to a rollback on a made-up docker (tests/fake_docker.py).

Nobody approves anything here. The diagnoses are written by hand for the tests; no model wrote them. Most tests
answer through a scripted terminal (ScriptedTerminal), a scripted approver standing in for Aravind's terminal.
A few run the real command: with no terminal, refused as Claude's shell is; in a pseudo-terminal the test makes,
which also shows the gate's limit (a process can make itself a terminal); and from a pipe, to see a waiting run's
notice at once.
"""

import getpass
import hashlib
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from klaxon.agent import gate
from klaxon.agent.contract import Diagnosis, Evidence, FixAction, ProposedFix
from klaxon.agent.tools import HOME
from klaxon.patient.deploys import DeployLog
from klaxon.patient.stack import StackError
from tests.fake_docker import GOOD, NAIVE, NOW, FakeDocker

LEDGER = "ledger-consumer"
ROLLBACK = FixAction("rollback_deploy", {"service": LEDGER})
ALERT = {"name": "MoneyCheckFailed", "summary": "The money check failed 1 of its 5 invariants.", "fired_at": None}


def made_up_diagnosis(fix_action=ROLLBACK, hypothesis=None):
    """Written by hand for the test: no model wrote it."""
    return Diagnosis(
        hypothesis=hypothesis or "A deploy of the ledger consumer without its dedupe books resent webhooks twice.",
        evidence=(
            Evidence("c1", "money_check", {"wait": True}, "7 events posted more than once"),
            Evidence("c2", "recent_deploys", {}, "ledger-consumer redeployed with LEDGER_HANDLER=naive"),
        ),
        confidence=0.8,
        proposed_fix=ProposedFix("rollback", LEDGER, "Roll the ledger consumer back to its idempotent handler."),
        blast_radius="The ledger pauses while its container restarts.",
        rollback_plan="Deploy the naive handler again.",
        fix_action=fix_action,
    )


class Clocks:
    """A wall clock and a monotonic clock that move only when the gate sleeps."""

    def __init__(self):
        self.mono, self.wall, self.slept = 0.0, NOW, []

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.mono += seconds
        self.wall += seconds


class ScriptedTerminal:
    """Stands in for Aravind's terminal: it answers from a script. A scripted approver, not a person."""

    def __init__(self, *answers, during=None):
        self.answers, self.shown, self.during = list(answers), [], during

    def write(self, text):
        self.shown.append(text)

    def ask(self, question):
        self.shown.append(question)
        if self.during is not None:
            self.during()
        return self.answers.pop(0) if self.answers else ""  # "": the end of input, no answer


def after_a_bad_deploy():
    docker = FakeDocker()
    deployer = docker.deployer()
    deployer.deploy(LEDGER, NAIVE, by="klaxon.faults")
    return docker, deployer


def saved_request(folder, clocks=None, diagnosis=None):
    """A request saved as a run saves it, for a rollback planned on a made-up docker."""
    from klaxon.agent import actions

    _, deployer = after_a_bad_deploy()
    clocks = clocks or Clocks()
    plan = actions.plan("rollback_deploy", {"service": LEDGER}, deployer)
    request = gate.make_request(plan, diagnosis or made_up_diagnosis(), folder, ALERT, clocks.time)
    assert gate._create_once(folder / gate.REQUEST, request)
    return request


def events(folder):
    return [json.loads(line)["event"] for line in (folder / gate.LOG).read_text().splitlines()]


def decision(folder):
    return json.loads((folder / gate.DECISION).read_text())


# The request


def test_a_request_holds_the_plan_code_made_and_apart_the_models_words_sealed_by_its_own_hash(tmp_path):
    request = saved_request(tmp_path)
    assert set(request) == gate.REQUEST_KEYS
    assert gate.REQUEST_ID.fullmatch(request["id"])
    assert (request["created_at"], request["expires_at"]) == ("2026-10-19T08:53:20Z", "2026-10-19T09:03:20Z")
    assert request["action"] == {"name": "rollback_deploy", "arguments": {"service": LEDGER}}
    assert request["plan"]["change"].startswith("redeploy ledger-consumer: LEDGER_HANDLER naive -> idempotent")
    assert request["diagnosis"]["evidence"][1] == {
        "call_id": "c2",
        "tool": "recent_deploys",
        "shows": "ledger-consumer redeployed with LEDGER_HANDLER=naive",
    }
    assert request["sha256"] == gate.request_sha256(request)
    assert gate.read_request(tmp_path) == request


def test_hidden_characters_and_the_home_folder_never_reach_a_request(tmp_path):
    words = f"Hidden‮ text, and a log line naming {HOME}/notes."
    request = saved_request(tmp_path, diagnosis=made_up_diagnosis(hypothesis=words))
    assert request["diagnosis"]["hypothesis"] == "Hidden text, and a log line naming ~/notes."


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda r: r["plan"].update(change="something harmless"), "was changed after the run saved it"),
        (lambda r: r.update(id="r-12345"), "has no valid id"),
        (lambda r: r.pop("diagnosis"), "is not an approval request"),
    ],
)
def test_a_request_changed_after_it_was_saved_is_refused(tmp_path, change, problem):
    request = saved_request(tmp_path)
    change(request)
    (tmp_path / gate.REQUEST).write_text(json.dumps(request))
    with pytest.raises(gate.GateError, match=problem):
        gate.read_request(tmp_path)


def test_a_folder_with_no_request_or_an_unreadable_one_is_refused(tmp_path):
    with pytest.raises(gate.GateError, match="holds no approval request"):
        gate.read_request(tmp_path)
    (tmp_path / gate.REQUEST).write_text("{not json")
    with pytest.raises(gate.GateError, match="cannot be read"):
        gate.read_request(tmp_path)


# make approve


def test_without_a_terminal_make_approve_refuses_and_writes_no_decision(tmp_path, capsys):
    saved_request(tmp_path)
    assert gate.approve(tmp_path, lambda: None, Clocks().time) == 2
    assert "this process has none. Approvals are typed by a person" in capsys.readouterr().err
    assert not (tmp_path / gate.DECISION).exists()
    assert events(tmp_path) == ["approval refused"]


def test_typing_the_requests_id_approves_it_and_the_decision_names_the_request_whole(tmp_path):
    request = saved_request(tmp_path)
    terminal = ScriptedTerminal(request["id"])
    assert gate.approve(tmp_path, lambda: terminal, Clocks().time) == 0
    written = decision(tmp_path)
    assert written["decision"] == "approved" and written["how"] == gate.TYPED
    assert (written["request_id"], written["request_sha256"]) == (request["id"], request["sha256"])
    assert events(tmp_path) == ["approved"]
    shown = "".join(terminal.shown)
    assert "change:  redeploy ledger-consumer: LEDGER_HANDLER naive -> idempotent" in shown
    assert "The diagnosis, in the model's own words (it read untrusted tool output: check it):" in shown
    assert f"Type {request['id']} to approve it. Anything else denies it: " in shown
    assert "Approved." in shown


@pytest.mark.parametrize("answer", ["", "\n", "y", "yes", "approve", "R-", "r-0000", "{id}x", "{ID}"])
def test_anything_but_the_requests_id_denies_it(tmp_path, answer):
    request = saved_request(tmp_path)
    typed = answer.replace("{id}", request["id"]).replace("{ID}", request["id"].upper())
    assert gate.approve(tmp_path, lambda: ScriptedTerminal(typed), Clocks().time) == 0
    assert decision(tmp_path)["decision"] == "denied" and events(tmp_path) == ["denied"]


def test_spaces_and_the_enter_key_around_the_id_still_approve(tmp_path):
    request = saved_request(tmp_path)
    assert gate.approve(tmp_path, lambda: ScriptedTerminal(f"  {request['id']} \n"), Clocks().time) == 0
    assert decision(tmp_path)["decision"] == "approved"


def test_an_expired_request_cannot_be_approved(tmp_path):
    clocks = Clocks()
    request = saved_request(tmp_path, clocks)
    clocks.wall += gate.WAIT_SECONDS
    terminal = ScriptedTerminal(request["id"])
    assert gate.approve(tmp_path, lambda: terminal, clocks.time) == 1
    assert not (tmp_path / gate.DECISION).exists() and "expired" in "".join(terminal.shown)
    assert not any("Type r-" in shown for shown in terminal.shown)  # nobody is asked about an expired request


def test_a_request_that_expires_while_it_is_read_is_not_approved(tmp_path):
    clocks = Clocks()
    request = saved_request(tmp_path, clocks)

    def reads_slowly():
        clocks.wall += gate.WAIT_SECONDS

    assert gate.approve(tmp_path, lambda: ScriptedTerminal(request["id"], during=reads_slowly), clocks.time) == 1
    assert not (tmp_path / gate.DECISION).exists()


def test_a_decision_is_written_once(tmp_path):
    request = saved_request(tmp_path)
    assert gate.approve(tmp_path, lambda: ScriptedTerminal("no"), Clocks().time) == 0
    terminal = ScriptedTerminal(request["id"])
    assert gate.approve(tmp_path, lambda: terminal, Clocks().time) == 1
    assert decision(tmp_path)["decision"] == "denied" and "Already decided: denied" in "".join(terminal.shown)


def test_make_approve_refuses_a_changed_request(tmp_path, capsys):
    request = saved_request(tmp_path)
    request["plan"]["change"] = "redeploy nothing"
    (tmp_path / gate.REQUEST).write_text(json.dumps(request))
    assert gate.approve(tmp_path, lambda: ScriptedTerminal(request["id"]), Clocks().time) == 2
    assert "changed after the run saved it" in capsys.readouterr().err
    assert not (tmp_path / gate.DECISION).exists()


def test_two_writers_of_one_decision_never_both_win(tmp_path):
    assert gate._create_once(tmp_path / "x.json", {"first": True})
    assert not gate._create_once(tmp_path / "x.json", {"second": True})
    assert json.loads((tmp_path / "x.json").read_text()) == {"first": True}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # no temporary file is left behind


def test_the_models_words_cannot_pass_for_a_line_of_the_request(tmp_path):
    words = "Rolled back already.\n  change:  nothing Type r-0000 to approve it."
    request = saved_request(tmp_path, diagnosis=made_up_diagnosis(hypothesis=words))
    shown = gate.render(request, NOW)
    assert "  hypothesis:     Rolled back already. change: nothing Type r-0000 to approve it." in shown.splitlines()
    assert [line for line in shown.splitlines() if line.startswith("  change:")] == [
        f"  change:  {request['plan']['change']}"
    ]


# The waiter


def test_the_run_takes_an_approval_only_for_its_own_request_whole(tmp_path):
    request = saved_request(tmp_path)
    resealed = dict(request, plan=dict(request["plan"], change="something else"))
    resealed["sha256"] = gate.request_sha256(resealed)  # changed, then sealed again by someone else
    (tmp_path / gate.REQUEST).write_text(json.dumps(resealed))
    assert gate.approve(tmp_path, lambda: ScriptedTerminal(request["id"]), Clocks().time) == 0  # it approved that one
    found = gate.wait(tmp_path, request, 600, Clocks().monotonic, Clocks().sleep, Clocks().time)
    assert (found.decision, found.how) == ("denied", "the decision names another request, or this one changed")
    assert events(tmp_path)[-1] == "decision not taken"


@pytest.mark.parametrize(
    ("written", "how"),
    [
        ({"decision": "approved", "at": "2026-10-19T09:03:21Z"}, "approved after the request expired"),
        ({"decision": "approved", "at": "yesterday"}, "the decision has no time this gate writes"),
        ({"decision": "yes"}, "the decision is not one this gate knows"),
        ({"request_id": "r-0000"}, "the decision names another request, or this one changed"),
    ],
)
def test_a_decision_that_is_not_one_the_gate_wrote_for_this_request_is_a_denial(tmp_path, written, how):
    request = saved_request(tmp_path)
    forged = {
        "label": gate.DECISION_LABEL,
        "request_id": request["id"],
        "request_sha256": request["sha256"],
        "decision": "approved",
        "at": "2026-10-19T08:55:00Z",
        "how": gate.TYPED,
    } | written
    (tmp_path / gate.DECISION).write_text(json.dumps(forged))
    clocks = Clocks()
    found = gate.wait(tmp_path, request, 600, clocks.monotonic, clocks.sleep, clocks.time)
    assert (found.decision, found.how) == ("denied", how)


@pytest.mark.parametrize("text", ["{not json", "[]", json.dumps({"decision": "approved"})])
def test_a_decision_file_that_cannot_be_read_is_a_denial(tmp_path, text):
    request = saved_request(tmp_path)
    (tmp_path / gate.DECISION).write_text(text)
    clocks = Clocks()
    found = gate.wait(tmp_path, request, 600, clocks.monotonic, clocks.sleep, clocks.time)
    assert (found.decision, found.how) == ("denied", "the decision file cannot be read")
    assert events(tmp_path) == ["decision not taken"]


def test_no_decision_within_ten_minutes_means_denied_and_the_gate_writes_so(tmp_path):
    request = saved_request(tmp_path)
    clocks = Clocks()
    found = gate.wait(tmp_path, request, gate.WAIT_SECONDS, clocks.monotonic, clocks.sleep, clocks.time)
    assert (found.decision, found.how) == ("expired", "no decision within 600 s")
    assert sum(clocks.slept) == gate.WAIT_SECONDS and max(clocks.slept) <= gate.POLL_SECONDS
    assert decision(tmp_path)["decision"] == "expired" and events(tmp_path) == ["expired"]


def test_a_decision_that_lands_as_the_wait_ends_is_the_one_that_counts(tmp_path, monkeypatch):
    request = saved_request(tmp_path)
    clocks = Clocks()
    real = gate._decision_bytes
    calls = []

    def lands_just_now(folder):
        calls.append(folder)
        if len(calls) == 1:  # "not yet", and the approval lands as the wait ends
            gate.approve(folder, lambda: ScriptedTerminal(request["id"]), clocks.time)
            clocks.mono += gate.WAIT_SECONDS
            return None
        return real(folder)

    monkeypatch.setattr(gate, "_decision_bytes", lands_just_now)
    found = gate.wait(tmp_path, request, gate.WAIT_SECONDS, clocks.monotonic, clocks.sleep, clocks.time)
    assert found.decision == "approved" and decision(tmp_path)["decision"] == "approved"


# From a diagnosis to a rollback


def act(folder, docker, deployer, diagnosis=None, answer="{id}", before=None, said=None):
    """act() with a scripted approver: the first time the run waits, it answers in make approve."""
    clocks = Clocks()
    answered = []

    def sleep(seconds):
        if not answered:
            answered.append(True)
            if before is not None:
                before()
            if answer is not None:
                request = gate.read_request(folder)
                typed = answer.replace("{id}", request["id"])
                gate.approve(folder, lambda: ScriptedTerminal(typed), clocks.time)
        clocks.sleep(seconds)

    return gate.act(
        folder,
        diagnosis or made_up_diagnosis(),
        deployer,
        ALERT,
        gate.WAIT_SECONDS,
        clocks.monotonic,
        sleep,
        clocks.time,
        (said if said is not None else []).append,
    )


def test_an_approved_rollback_runs_and_every_step_is_logged(tmp_path):
    docker, deployer = after_a_bad_deploy()
    said = []
    outcome = act(tmp_path, docker, deployer, said=said)
    request = gate.read_request(tmp_path)
    assert (outcome.result, outcome.request_id) == ("done", request["id"])
    assert docker.settings(LEDGER) == GOOD[LEDGER]
    latest = DeployLog().latest(LEDGER)
    assert latest.by == f"Klaxon, approved request {request['id']}" and latest.replaced_settings == NAIVE
    assert events(tmp_path) == ["requested", "approved", "decision taken", "running", "done"]
    assert f"Klaxon asks to act: request {request['id']}" in said[0]
    assert f"make approve RUN={tmp_path}" in said[0]  # a temporary folder: outside the home folder


@pytest.mark.parametrize(("answer", "result"), [("no", "denied"), (None, "expired")])
def test_a_denied_or_unanswered_request_runs_nothing(tmp_path, answer, result):
    docker, deployer = after_a_bad_deploy()
    ups = len(docker.ups())
    outcome = act(tmp_path, docker, deployer, answer=answer)
    assert outcome.result == result and len(docker.ups()) == ups and docker.settings(LEDGER) == NAIVE
    # make approve logs the denial, then the run logs the decision it took; an expiry, the run writes and logs
    assert events(tmp_path) == (["requested", "denied", "decision taken"] if answer else ["requested", "expired"])


def test_a_stack_that_changed_while_the_request_waited_is_not_touched(tmp_path):
    docker, deployer = after_a_bad_deploy()
    ups = len(docker.ups())
    outcome = act(tmp_path, docker, deployer, before=lambda: docker.start(LEDGER, NAIVE))
    assert outcome.result == "not run" and "changed since the request was made" in outcome.detail
    assert len(docker.ups()) == ups
    assert events(tmp_path) == ["requested", "approved", "decision taken", "running", "not run"]


def test_a_rollback_that_fails_in_docker_is_logged_as_failed(tmp_path):
    docker, deployer = after_a_bad_deploy()

    def breaks():
        docker.up_fails = StackError("the container did not turn healthy")

    outcome = act(tmp_path, docker, deployer, before=breaks)
    assert outcome.result == "failed" and "did not turn healthy" in outcome.detail
    assert events(tmp_path)[-1] == "failed"


def test_a_diagnosis_that_names_no_action_asks_nothing(tmp_path):
    docker, deployer = after_a_bad_deploy()
    outcome = act(tmp_path, docker, deployer, diagnosis=made_up_diagnosis(fix_action=None))
    assert outcome.result == "no action" and not (tmp_path / gate.REQUEST).exists()
    assert events(tmp_path) == ["no action"]


def test_an_action_that_cannot_be_planned_is_refused_and_nobody_is_asked(tmp_path):
    docker = FakeDocker()  # nothing deployed: nothing to roll back to
    outcome = act(tmp_path, docker, docker.deployer())
    assert outcome.result == "refused" and "has no deploy of ledger-consumer" in outcome.detail
    assert not (tmp_path / gate.REQUEST).exists() and events(tmp_path) == ["refused"]


def test_one_request_per_run_folder(tmp_path):
    docker, deployer = after_a_bad_deploy()
    act(tmp_path, docker, deployer, answer="no")
    with pytest.raises(gate.GateError, match="already holds an approval request"):
        act(tmp_path, docker, deployer)


def test_no_gate_file_holds_the_macs_user_name_or_home_folder(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    docker, deployer = after_a_bad_deploy()
    act(folder, docker, deployer)
    texts = [path.read_text() for path in folder.iterdir()] + [DeployLog().path.read_text()]
    user = getpass.getuser()
    for text in texts:
        assert HOME not in text
        assert len(user) < 3 or user not in text


# The real command: no terminal, then a pseudo-terminal


def test_make_approve_with_no_terminal_is_refused_as_claudes_shell_is(tmp_path):
    saved_request(tmp_path)
    done = subprocess.run(
        [sys.executable, "-m", "klaxon.agent.gate", "approve", str(tmp_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,  # no controlling terminal either
    )
    assert done.returncode == 2 and "Refused: make approve reads the answer from a terminal" in done.stderr
    assert not (tmp_path / gate.DECISION).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal needs a Unix")
def test_make_approve_in_a_terminal_approves_by_the_typed_id(tmp_path):
    """A pseudo-terminal the test makes, standing in for Aravind's: the command reads from /dev/tty, and the id
    typed into it approves. It also shows the limit the docs state: a process can make itself a terminal."""
    import pty

    clocks = Clocks()
    clocks.wall = time.time()  # the command reads the real clock: the request is made now
    request = saved_request(tmp_path, clocks)
    pid, fd = pty.fork()
    if pid == 0:  # the child: the command, with the pseudo-terminal as its terminal
        os.execv(sys.executable, [sys.executable, "-m", "klaxon.agent.gate", "approve", str(tmp_path)])
    shown = _read_until(fd, f"Type {request['id']}".encode())
    os.write(fd, f"{request['id']}\n".encode())
    shown += _read_until(fd, b"")
    assert _exit_code(pid) == 0, shown
    assert decision(tmp_path)["decision"] == "approved" and decision(tmp_path)["how"] == gate.TYPED
    assert "Approved." in shown


def _exit_code(pid, seconds=30.0):
    """The child's exit code; None when it had not ended in time, and was killed: a test never hangs on it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.05)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    return None


def _read_until(fd, marker, seconds=30.0):
    """What the pseudo-terminal shows, until marker, or any of several (or its end, for b"")."""
    markers = marker if isinstance(marker, tuple) else (marker,)
    out, deadline = b"", time.monotonic() + seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # the child closed it
            break
        if not chunk:
            break
        out += chunk
        if any(m and m in out for m in markers):
            break
    return out.decode(errors="replace")


def test_how_to_answer_names_the_command_with_the_home_folder_as_tilde(tmp_path):
    request = saved_request(tmp_path)
    text = gate.how_to_answer(Path(HOME) / "klaxon" / "evidence" / "run", request)
    assert "make approve RUN=~/klaxon/evidence/run" in text and HOME not in text


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal needs a Unix")
def test_a_terminal_is_not_enough_the_commands_own_input_must_be_one_too(tmp_path):
    """`echo r-1234 | make approve`, run from a terminal: refused, though /dev/tty would open."""
    import pty

    clocks = Clocks()
    clocks.wall = time.time()
    saved_request(tmp_path, clocks)
    pid, fd = pty.fork()
    if pid == 0:  # the child has the pseudo-terminal, but its input comes from elsewhere
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.execv(sys.executable, [sys.executable, "-m", "klaxon.agent.gate", "approve", str(tmp_path)])
    shown = _read_until(fd, (b"Refused", b"Type r-"))  # either it refuses, or it asks: then it is ended at once
    assert _exit_code(pid, 10 if "Refused" in shown else 0.1) == 2 and "Refused" in shown
    assert not (tmp_path / gate.DECISION).exists()


# The demo: a simulation, answered here by a scripted approver


@pytest.mark.parametrize(("answer", "result", "handler"), [("{id}", "done", "idempotent"), (None, "expired", "naive")])
def test_the_gate_demo_is_a_simulation_that_touches_no_real_stack_or_log(
    tmp_path, monkeypatch, capsys, answer, result, handler
):
    from klaxon.demo import gate_demo

    monkeypatch.setattr(gate_demo.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    asked = []

    def sleep(seconds):
        if answer is not None and not asked:
            asked.append(True)
            request = gate.read_request(tmp_path)
            gate.approve(tmp_path, lambda: ScriptedTerminal(answer.replace("{id}", request["id"])), time.time)

    assert gate_demo.main(timeout=0.5 if answer is None else 60, sleep=sleep) == 0
    out = capsys.readouterr().out
    assert out.startswith(gate_demo.BANNER) and f"Outcome: {result}" in out
    assert f"The made-up ledger consumer now runs LEDGER_HANDLER={handler}." in out
    assert not DeployLog().path.exists()  # the real log (here, this test's own) is never written: the demo has one
    assert len(DeployLog(tmp_path / "deploys.jsonl").records()) == (2 if result == "done" else 1)


def test_a_waiting_run_says_how_to_answer_at_once_even_into_a_pipe(tmp_path):
    """Whoever watches a run (a terminal, or Claude reading its output) must see the request while it waits. Read
    with a deadline: output held in a buffer would show only when the run ends, ten minutes later."""
    child = subprocess.Popen(
        [sys.executable, "-m", "klaxon.demo.gate_demo"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=os.environ | {"TMPDIR": str(tmp_path)},  # its folder lands here
    )
    try:
        shown, deadline = b"", time.monotonic() + 30
        while time.monotonic() < deadline and b"make approve RUN=" not in shown:
            ready, _, _ = select.select([child.stdout], [], [], 0.2)
            if ready:
                shown += os.read(child.stdout.fileno(), 4096)
        text = shown.decode()
        assert "Klaxon asks to act: request r-" in text and f"make approve RUN={tmp_path}" in text, text
        assert child.poll() is None  # still waiting for a decision
    finally:
        child.kill()
        child.wait()


def test_a_mac_that_sleeps_through_a_request_finds_it_expired_when_it_wakes(tmp_path):
    """A Mac's monotonic clock stands still while it sleeps; the wall clock does not. The gate demo, left waiting,
    once waited on a request open until 10:13 until 18:46."""
    request = saved_request(tmp_path)
    clocks = Clocks()

    def the_mac_sleeps(seconds):
        if clocks.slept:  # a second sleep: the waiter did not see the wall clock pass the expiry (never hang)
            raise AssertionError("the waiter slept again after the request expired by the wall clock")
        clocks.slept.append(seconds)
        clocks.wall += 8 * 3600  # eight hours pass on the wall; the monotonic clock does not move

    found = gate.wait(tmp_path, request, gate.WAIT_SECONDS, clocks.monotonic, the_mac_sleeps, clocks.time)
    assert found.decision == "expired" and len(clocks.slept) == 1
    assert decision(tmp_path)["decision"] == "expired"


def test_the_run_logs_the_decision_it_takes_so_one_not_written_by_make_approve_shows(tmp_path):
    """The stated limit's case: a process writes the decision file itself. The gate cannot stop it, but its log
    shows that the run took an approval with no line from make approve before it, and the file's own hash."""
    docker, deployer = after_a_bad_deploy()

    def forged():
        request = gate.read_request(tmp_path)
        written = {
            "label": gate.DECISION_LABEL,
            "request_id": request["id"],
            "request_sha256": request["sha256"],
            "decision": "approved",
            "at": request["created_at"],
            "how": gate.TYPED,  # what the file says; anyone can write it
        }
        (tmp_path / gate.DECISION).write_text(json.dumps(written))

    outcome = act(tmp_path, docker, deployer, answer=None, before=forged)
    assert outcome.result == "done"
    lines = [json.loads(line) for line in (tmp_path / gate.LOG).read_text().splitlines()]
    assert [line["event"] for line in lines] == ["requested", "decision taken", "running", "done"]
    taken = lines[1]
    assert taken["decision"] == "approved" and len(taken["decision_sha256"]) == 64
    assert "approved" not in [line["event"] for line in lines]  # no line from make approve: not written by it
    assert taken["decision_sha256"] == hashlib.sha256((tmp_path / gate.DECISION).read_bytes()).hexdigest()


def test_a_refused_make_approve_writes_no_log_outside_a_run_folder(tmp_path):
    assert gate.approve(tmp_path, lambda: None, Clocks().time) == 2
    assert list(tmp_path.iterdir()) == []


def test_a_request_sealed_again_in_a_shape_a_run_never_writes_is_refused_cleanly(tmp_path, capsys):
    request = saved_request(tmp_path)
    del request["plan"]["undoes"]
    request["sha256"] = gate.request_sha256(request)
    (tmp_path / gate.REQUEST).write_text(json.dumps(request))
    assert gate.approve(tmp_path, lambda: ScriptedTerminal(request["id"]), Clocks().time) == 2
    assert "not in the shape a run writes" in capsys.readouterr().err
    assert not (tmp_path / gate.DECISION).exists()


def test_anything_that_breaks_while_the_action_runs_is_logged_then_raised(tmp_path):
    docker, deployer = after_a_bad_deploy()

    def breaks():
        docker.up_fails = OSError("the disk is full")

    with pytest.raises(OSError, match="the disk is full"):
        act(tmp_path, docker, deployer, before=breaks)
    last = json.loads((tmp_path / gate.LOG).read_text().splitlines()[-1])
    assert last["event"] == "failed" and last["why"] == "OSError: the disk is full"


def test_the_request_shown_says_it_was_planned_when_the_run_asked(tmp_path):
    request = saved_request(tmp_path)
    assert "The action, planned by code from the stack as it was when the run asked:" in gate.render(request, NOW)


def test_make_approve_logs_the_hash_of_the_bytes_it_wrote_and_the_run_logs_the_same_when_it_takes_them(tmp_path):
    docker, deployer = after_a_bad_deploy()
    act(tmp_path, docker, deployer)
    lines = [json.loads(line) for line in (tmp_path / gate.LOG).read_text().splitlines()]
    approved, taken = (
        next(line for line in lines if line["event"] == event) for event in ("approved", "decision taken")
    )
    on_file = hashlib.sha256((tmp_path / gate.DECISION).read_bytes()).hexdigest()
    assert approved["decision_sha256"] == taken["decision_sha256"] == on_file


def test_a_request_sealed_again_without_its_confidence_is_refused_cleanly(tmp_path, capsys):
    request = saved_request(tmp_path)
    del request["diagnosis"]["confidence"]
    request["sha256"] = gate.request_sha256(request)
    (tmp_path / gate.REQUEST).write_text(json.dumps(request))
    assert gate.approve(tmp_path, lambda: ScriptedTerminal(request["id"]), Clocks().time) == 2
    assert "not in the shape a run writes" in capsys.readouterr().err


# Week 4, item 2: an undo proposed after a failed verification, and the decision the run took, for its evidence


def done_rollback(folder):
    """An approved rollback that ran, after a fault's bad deploy, answered by a scripted approver."""
    docker, deployer = after_a_bad_deploy()
    outcome = act(folder, docker, deployer)
    assert outcome.result == "done"
    return docker, deployer, outcome


def test_a_failed_verification_proposes_the_undo_and_the_gate_refuses_it_as_a_persons_call(tmp_path):
    docker, deployer, done = done_rollback(tmp_path)
    ups = len(docker.ups())
    outcome = gate.propose_undo(tmp_path, done, deployer, "the verification failed: made up", Clocks().time)
    assert (outcome.result, outcome.request_id) == ("refused", done.request_id)
    assert outcome.detail == (
        "Klaxon will not undo it: rolling ledger-consumer back would deploy a known-bad setting (LEDGER_HANDLER=naive):"
        " Klaxon never deploys one; a person can, by hand. Undoing it is a person's call: Klaxon asks nobody to"
        " approve it."
    )
    assert len(docker.ups()) == ups and docker.settings(LEDGER) == GOOD[LEDGER]  # nothing ran
    assert events(tmp_path)[-2:] == ["undo proposed", "undo refused"]
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith("approval-request")] == [gate.REQUEST]


def test_an_undo_is_refused_once_the_service_was_deployed_again_since_klaxons_rollback(tmp_path):
    docker, deployer, done = done_rollback(tmp_path)
    deployer.deploy(LEDGER, NAIVE, by="klaxon.faults")  # someone deployed since: that deploy is not Klaxon's
    outcome = gate.propose_undo(tmp_path, done, deployer, "made up", Clocks().time)
    assert outcome.result == "refused" and "was deployed again since Klaxon's rollback" in outcome.detail


def test_an_undo_is_refused_once_the_container_was_recreated_outside_the_log(tmp_path):
    docker, deployer, done = done_rollback(tmp_path)
    docker.start(LEDGER, GOOD[LEDGER])  # a recreate by hand: the log's latest line is still Klaxon's
    outcome = gate.propose_undo(tmp_path, done, deployer, "made up", Clocks().time)
    assert outcome.result == "refused" and "recreated outside the log" in outcome.detail


def test_an_undo_klaxon_could_run_is_refused_too_because_its_request_is_not_built(tmp_path, monkeypatch):
    """Not this system: with no known-bad values, undoing the rollback would be a rollback Klaxon may make. It still
    runs nothing, and asks nobody: an approval request for an undo, saying why, is not built."""
    from klaxon.patient import deploys

    docker, deployer, done = done_rollback(tmp_path)
    ups = len(docker.ups())
    monkeypatch.setattr(deploys, "KNOWN_BAD", {})
    outcome = gate.propose_undo(tmp_path, done, deployer, "made up", Clocks().time)
    assert outcome.result == "refused" and "an approval request for an undo is not built" in outcome.detail
    assert gate.PERSONS_CALL in outcome.detail
    assert len(docker.ups()) == ups and docker.settings(LEDGER) == GOOD[LEDGER]
    assert events(tmp_path)[-1] == "undo refused"


@pytest.mark.parametrize("result", ["denied", "expired", "not run", "failed", "refused", "no action"])
def test_only_an_action_that_ran_can_be_undone(tmp_path, result):
    docker, deployer = after_a_bad_deploy()
    with pytest.raises(gate.GateError, match="only an action that ran"):
        gate.propose_undo(tmp_path, gate.Outcome(result, "made up", "r-0000"), deployer, "made up")


def test_an_undo_of_a_deploy_that_names_no_known_service_is_refused(tmp_path):
    from klaxon.agent import actions

    _, deployer = after_a_bad_deploy()
    for done in ({}, {"service": "postgres", "seq": 1}, None):
        with pytest.raises(actions.ActionRefused, match="there is no deploy of Klaxon's to undo"):
            actions.plan_undo(done, deployer)


def test_the_run_says_whether_make_approve_wrote_the_decision_it_took(tmp_path):
    done_rollback(tmp_path)
    found = gate.decided(tmp_path)
    assert found["decision"] == "approved" and found["how"] == gate.TYPED and found["written_by_make_approve"] is True
    assert found["at"] == decision(tmp_path)["at"]


def test_a_decision_no_line_of_make_approve_names_is_said_as_such(tmp_path):
    docker, deployer = after_a_bad_deploy()

    def forged():
        request = gate.read_request(tmp_path)
        written = {
            "label": gate.DECISION_LABEL,
            "request_id": request["id"],
            "request_sha256": request["sha256"],
            "decision": "approved",
            "at": request["created_at"],
            "how": gate.TYPED,
        }
        (tmp_path / gate.DECISION).write_text(json.dumps(written))

    act(tmp_path, docker, deployer, answer=None, before=forged)
    assert gate.decided(tmp_path)["written_by_make_approve"] is False


def test_with_no_decision_taken_there_is_none_to_say(tmp_path):
    assert gate.decided(tmp_path) is None  # no log at all
    docker, deployer = after_a_bad_deploy()
    act(tmp_path, docker, deployer, answer=None)  # expired: the run's own decision, taken by no one
    assert gate.decided(tmp_path) is None
    (tmp_path / gate.LOG).write_text("{not json\n")
    with pytest.raises(gate.GateError, match="cannot be read"):
        gate.decided(tmp_path)


def test_a_run_stopped_while_it_waits_closes_its_request_so_no_later_yes_counts(tmp_path):
    # The review's L6: an interrupted wait left the request open, so make approve still offered it and logged a yes.
    docker, deployer = after_a_bad_deploy()

    def stopped(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        gate.act(tmp_path, made_up_diagnosis(), deployer, ALERT, sleep=stopped, now=Clocks().time, say=[].append)
    closed = decision(tmp_path)
    assert (closed["decision"], closed["how"]) == ("denied", gate.STOPPED)
    assert events(tmp_path) == ["requested", "stopped"]
    request = gate.read_request(tmp_path)
    terminal = ScriptedTerminal(request["id"])
    assert gate.approve(tmp_path, lambda: terminal, Clocks().time) == 1  # nothing left to decide
    assert "Already decided: denied" in "".join(terminal.shown) and events(tmp_path) == ["requested", "stopped"]
    assert docker.settings(LEDGER) == NAIVE


def test_a_decision_that_landed_before_the_run_stopped_stands_as_written(tmp_path):
    docker, deployer = after_a_bad_deploy()
    clocks = Clocks()

    def approved_then_stopped(seconds):
        gate.approve(tmp_path, lambda: ScriptedTerminal(gate.read_request(tmp_path)["id"]), clocks.time)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        gate.act(
            tmp_path, made_up_diagnosis(), deployer, ALERT, sleep=approved_then_stopped, now=clocks.time, say=[].append
        )
    assert decision(tmp_path)["decision"] == "approved"  # written first: it stands, and no run took it
    assert events(tmp_path) == ["requested", "approved"]
    assert docker.settings(LEDGER) == NAIVE  # nothing ran


def test_the_caller_hears_right_before_a_request_and_only_when_a_plan_was_made(tmp_path):
    # The review's L1: the run reads the money right before the request, never for a plan the gate refused.
    heard = []
    _, deployer = after_a_bad_deploy()

    def before_asking():
        heard.append((tmp_path / "asked" / gate.REQUEST).exists())

    cases = [
        ("asked", made_up_diagnosis(), deployer),
        ("refused", made_up_diagnosis(), FakeDocker().deployer()),  # nothing deployed: nothing to roll back to
        ("no action", made_up_diagnosis(fix_action=None), deployer),
    ]
    for name, diagnosis, planned_on in cases:
        (tmp_path / name).mkdir()
        gate.act(tmp_path / name, diagnosis, planned_on, ALERT, timeout=0.0, say=[].append, before_asking=before_asking)
    assert heard == [False]  # once, for the plan that was made, before its request was saved
