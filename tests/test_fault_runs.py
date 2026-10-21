"""The harness for the loop's runs on the faults, offline: the order of things (traffic, a quiet stack, the fault, the
alert, the loop, the fault cleared, the traffic stopped); that the loop gets exactly the alert the rules raised; that
the fault is always cleared and the traffic always stopped; and that nothing the model is given names the fault.

The stack, the traffic and the readings are made up here, and so is every model reply: the command's end-to-end test
runs Klaxon's real loop with scripted replies, and Klaxon's real MCP server code serving made-up tool results."""

import json
import os
import re
import signal
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from klaxon import alerts, fault_runs, faults, grading
from klaxon.agent import claude_code, gate, loop, verify
from klaxon.agent import mcp_client as mc
from klaxon.agent.claude_code import find_claude
from klaxon.agent.contract import Diagnosis, Evidence, FixAction, ProposedFix
from klaxon.agent.model import ModelError, ScriptedModel, text_sha256
from klaxon.agent.tools import make_tools
from klaxon.agent.trace import read_trace
from klaxon.checks import live
from klaxon.checks.money import InvariantResult, MoneyReport
from klaxon.patient.deploys import Deploy, DeployError
from klaxon.patient.provider import Event
from klaxon.patient.stack import StackError
from klaxon.traffic import Settings
from tests.agent.test_gate import ScriptedTerminal
from tests.agent.test_loop import Served, tool
from tests.conftest import NO_APP_COPIES, NO_CLAUDE
from tests.fake_docker import GOOD, FakeDocker
from tests.test_alerts import FAILED, HEALTHY, PASSED

NOW = 1_792_360_000.0
ALERTING, STACK = alerts.Alerting, fault_runs.Stack  # the real ones: some tests put a made-up stack's in their place
SLOW = {"p95_seconds": [({}, 9.37)]}


class FakeTraffic:
    def __init__(self, world, settings):
        self.world, self.settings = world, settings
        self.stats = type(
            "Stats", (), {"to_dict": lambda self: {"counts": {"payments.succeeded": 3}, "unexpected": []}}
        )()
        self.behavior_note = "the provider's behavior was put back as it was found"

    def start(self):
        if self.world.traffic_fails:
            raise httpx.ConnectError("the provider is out of reach")
        self.world.calls.append("traffic on")

    def close(self):
        self.world.calls.append("traffic closed")

    def join(self):
        self.world.calls.append("traffic joined")


class World:
    """A made-up stack. Healthy, with the readings `before` changes; while the fault is in place, `sick` too."""

    def __init__(self, sick=None, money_sick=PASSED, before=None, money_before=PASSED):
        self.now = 0.0
        self.calls = []
        self.fault_on = False
        self.sick, self.money_sick = (SLOW if sick is None else sick), money_sick
        self.before, self.money_before = before or {}, money_before
        self.traffic_fails = False
        self.inject_fails = self.clear_fails = False
        self.money_takes = 0.0  # seconds each money check takes (a stalled consumer: up to 15)
        self.lingers = False  # the symptoms outlast the clearing

    def prometheus(self, query):
        series = HEALTHY | self.before | (self.sick if self.fault_on else {})
        answer = series[next(name for name, text in alerts.QUERIES.items() if text == query)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def money(self):
        self.now += self.money_takes
        return dict(self.money_sick if self.fault_on else self.money_before)

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now

    def inject(self, local):
        self.calls.append("inject")
        self.fault_on = True
        if self.inject_fails:
            raise RuntimeError("the redeploy failed halfway")
        return {"made_up": "injected"}

    def clear(self, local):
        self.calls.append("clear")
        if self.clear_fails:
            raise RuntimeError("docker did not answer")
        self.fault_on = self.lingers
        return {"made_up": "cleared"}

    def alerting(self):
        return ALERTING(self.prometheus, self.money, clock=lambda: NOW + self.now)

    def stack(self):
        return STACK(None, self.alerting(), lambda s: FakeTraffic(self, s), self.sleep, self.clock)

    def fault(self, name="provider-outage"):
        return replace(faults.FAULTS[name], inject=self.inject, clear=self.clear)


DIAGNOSIS = Diagnosis(
    "The payment provider is timing out, so the circuit breaker opened and every payment fails fast with a 503.",
    (Evidence("c1", "query_metrics", {"query": "x"}, "made up"),),
    0.7,
    ProposedFix("none", "fake-provider", "Wait for the provider; nothing to change on our side."),
    "none",
    "none",
)


def diagnosed(world, seconds=120.0):
    def investigate(alert):
        world.calls.append(("investigate", alert))
        world.now += seconds
        return loop.RunResult("diagnosed", "confidence 0.7", DIAGNOSIS, 5, 4, seconds, Path("trace.jsonl"))

    return investigate


def record_for(fault):
    return fault_runs.Record(fault.describe(), {"rate": 3.0}, {"steps": 12})


def run(world, investigate=None, fault=None, **kwargs):
    fault = fault or world.fault()
    record = record_for(fault)
    out = fault_runs.run(fault, record, world.stack(), Settings(run_id="r1"), investigate or diagnosed(world), **kwargs)
    return out


def calls(world):
    return [call if isinstance(call, str) else call[0] for call in world.calls]


# A run, in order


def test_a_run_goes_from_a_quiet_stack_to_the_alert_to_the_loop_and_then_clears_the_fault():
    world = World()
    record = run(world)
    assert calls(world) == ["traffic on", "inject", "investigate", "clear", "traffic closed", "traffic joined"]
    assert [e["phase"] for e in record.evaluations] == ["before", "before", "during", "during", "after"]
    assert record.alert["name"] == "PaymentsLatencyHigh" and record.stopped == "the loop stopped: diagnosed"
    assert record.injected == {"made_up": "injected"} and record.cleared == {"made_up": "cleared"}
    assert record.timings == {
        "injected_at": 75.0,
        "injection_took": 0.0,
        "alert_evaluation_began": 105.0,
        "loop_started_at": 105.0,
        "loop_ended_at": 225.0,
        "cleared_at": 225.0,
    }
    assert record.evaluations[-1]["began"] == 225.0 + fault_runs.AFTER_SECONDS
    assert record.traffic == {"counts": {"payments.succeeded": 3}, "unexpected": []}


def test_the_loop_gets_exactly_the_alert_the_rules_raised():
    world = World()
    raised = []
    stack = world.stack()
    evaluate = stack.alerting.evaluate

    def spy():
        evaluation = evaluate()
        raised.append(evaluation.alert)
        return evaluation

    stack.alerting.evaluate = spy
    record = record_for(world.fault())
    fault_runs.run(world.fault(), record, stack, Settings(run_id="r1"), diagnosed(world))
    [(_, given)] = [call for call in world.calls if not isinstance(call, str)]
    assert given is [alert for alert in raised if alert is not None][0]
    assert record.alert == {"name": given.name, "summary": given.summary, "fired_at": given.fired_at}


def test_the_diagnosis_is_compared_with_the_faults_recorded_cause_by_the_rule():
    world = World()
    record = run(world)
    assert record.comparison == grading.compare("provider-outage", DIAGNOSIS.as_dict())
    assert record.comparison["verdict"] == "names the cause" and record.comparison["by"] == grading.BY[2]
    assert record.comparison["rubric"]["version"] == 2 and record.comparison["fix_fits_the_remedy"] is True
    assert record.loop == {
        "reason": "diagnosed",
        "detail": "confidence 0.7",
        "steps": 5,
        "tool_calls": 4,
        "seconds": 120.0,
        "diagnosis": DIAGNOSIS.as_dict(),
        "trace": "trace.jsonl",
    }


# Never on an unwell stack


@pytest.mark.parametrize(
    "before, money_before, why",
    [
        (SLOW, PASSED, "a rule's condition already holds: PaymentsLatencyHigh"),
        ({}, FAILED, "a rule's condition already holds: MoneyCheckFailed"),
        ({}, {"outcome": "could not settle"}, "the money check did not pass: could not settle"),
        ({}, {"outcome": "could not run", "error": "ConnectError"}, "the money check could not run (ConnectError)"),
        ({"requests_per_second": httpx.ConnectError("x")}, PASSED, "Prometheus could not be read (ConnectError)"),
        ({"p95_seconds": [], "requests_per_second": []}, PASSED, "no data for the p95 latency, the share of 5xx"),
        ({"lag_by_group": [({"consumer": "ledger"}, 0.0)]}, PASSED, "no data for the payment-confirmer group's lag"),
    ],
)
def test_no_fault_is_injected_on_a_stack_that_is_not_quiet(before, money_before, why):
    world = World(before=before, money_before=money_before)
    record = run(world)
    assert calls(world) == ["traffic on", "traffic closed", "traffic joined"]
    assert record.stopped.startswith("not quiet before the fault, so none was injected: ")
    assert why in record.stopped
    assert record.injected is None and record.cleared is None and "injected_at" not in record.timings


def test_the_stack_must_be_quiet_at_two_evaluations_in_a_row():
    world = World()
    stack = world.stack()
    seen = []
    evaluate = stack.alerting.evaluate

    def blip_on_the_second():
        seen.append(1)
        world.before = SLOW if len(seen) == 2 else {}
        return evaluate()

    stack.alerting.evaluate = blip_on_the_second
    record = record_for(world.fault())
    fault_runs.run(world.fault(), record, stack, Settings(run_id="r1"), diagnosed(world))
    assert "inject" not in world.calls and len(seen) == 2


def test_the_rules_read_the_stack_only_once_the_traffic_has_warmed_up():
    world = World()
    record = run(world)
    assert record.evaluations[0]["began"] == fault_runs.WARMUP_SECONDS


# The fault is always cleared, and the traffic always stopped


def test_no_alert_in_time_means_no_loop_and_the_fault_is_still_cleared():
    world = World(sick={})  # the fault shows nothing the rules read
    record = run(world)
    assert calls(world) == ["traffic on", "inject", "clear", "traffic closed", "traffic joined"]
    assert record.stopped == "no alert fired within 180 s of the injection"
    assert record.alert is None and record.loop is None and record.comparison is None
    during = [e for e in record.evaluations if e["phase"] == "during"]
    assert len(during) == 12 and during[-1]["began"] - record.timings["injected_at"] == 180.0


@pytest.mark.parametrize("error", [RuntimeError("the loop crashed"), KeyboardInterrupt()])
def test_the_fault_is_cleared_and_the_traffic_stopped_even_when_the_loop_fails(error):
    world = World()

    def failing(alert):
        world.calls.append(("investigate", alert))
        raise error

    with pytest.raises(type(error)):
        run(world, investigate=failing)
    assert calls(world) == ["traffic on", "inject", "investigate", "clear", "traffic closed", "traffic joined"]


def test_an_injection_that_failed_halfway_is_cleared_too():
    world = World()
    world.inject_fails = True
    with pytest.raises(RuntimeError, match="halfway"):
        run(world)
    assert calls(world) == ["traffic on", "inject", "clear", "traffic closed", "traffic joined"]


def test_the_traffic_is_stopped_even_when_clearing_fails():
    world = World()
    world.clear_fails = True
    with pytest.raises(RuntimeError, match="docker did not answer"):
        run(world)
    assert calls(world)[-2:] == ["traffic closed", "traffic joined"]


def test_traffic_that_cannot_start_touches_nothing():
    world = World()
    world.traffic_fails = True
    with pytest.raises(httpx.ConnectError):
        run(world)
    assert world.calls == []


# Traffic with no fault


def test_with_no_fault_the_rules_are_read_again_and_again_and_counted():
    world = World()
    found = fault_runs.quiet(
        world.alerting(), Settings(run_id="r1"), 1.0, lambda s: FakeTraffic(world, s), world.sleep, world.clock
    )
    assert calls(world) == ["traffic on", "traffic closed", "traffic joined"]
    assert [e["began"] for e in found["evaluations"]] == [
        60.0,
        75.0,
        90.0,
        105.0,
        120.0,
    ]  # the minute after the warm-up
    assert (found["alerts_fired"], found["conditions_held"], found["evaluations_with_problems"]) == (0, 0, 0)


def test_with_no_fault_a_condition_that_holds_is_counted():
    world = World(before=SLOW)
    found = fault_runs.quiet(
        world.alerting(), Settings(run_id="r1"), 1.0, lambda s: FakeTraffic(world, s), world.sleep, world.clock
    )
    assert (found["alerts_fired"], found["conditions_held"]) == (4, 5)  # it fires from the second evaluation on


# Before the stack is touched


class Tools:
    def __init__(self, extra=None):
        self.closed = False
        self.extra = extra

    def open(self, timeout=None):
        specs = [mc.ToolSpec(t.name, t.title, t.description, t.input_schema, READ_ONLY) for t in make_tools().values()]
        return specs + ([self.extra] if self.extra else [])

    def close(self):
        self.closed = True


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}


def test_preflight_checks_the_model_claude_code_and_the_tools(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("KLAXON_CLAUDE_BIN", str(fake))
    tools = Tools()
    assert fault_runs.preflight("claude-opus-5-5", lambda: tools) == fake and tools.closed
    with pytest.raises(ValueError, match="full model id"):
        fault_runs.preflight("opus", lambda: Tools())
    offered = Tools(mc.ToolSpec("restart_service", "Restart", "x", {"type": "object"}, READ_ONLY))
    with pytest.raises(loop.ToolsRefused, match="not on the allow-list"):
        fault_runs.preflight("claude-opus-5-5", lambda: offered)
    assert offered.closed
    monkeypatch.setenv("KLAXON_CLAUDE_BIN", str(tmp_path / "nothing-here"))
    with pytest.raises(ModelError):
        fault_runs.preflight("claude-opus-5-5", lambda: Tools())


def test_no_test_can_find_the_real_claude_code():
    # tests/conftest.py: whatever a mutant breaks, an offline test can never reach Aravind's seat.
    assert os.environ["KLAXON_CLAUDE_BIN"] == NO_CLAUDE
    with pytest.raises(ModelError, match="not an executable file"):
        find_claude()
    # Finding 8: even code that skipped KLAXON_CLAUDE_BIN finds no desktop-app copy, with the mutation runner's PATH.
    assert claude_code.APP_COPIES == NO_APP_COPIES
    with pytest.raises(ModelError, match="Claude Code was not found"):
        find_claude({"PATH": "/usr/bin:/bin"})


def test_a_run_that_cannot_start_touches_nothing_and_leaves_no_folder(monkeypatch, tmp_path, capsys):
    def untouchable(*args, **kwargs):
        raise AssertionError("touched the stack")

    monkeypatch.setattr(fault_runs.faults, "LocalStack", untouchable)
    monkeypatch.setattr(fault_runs.traffic, "TrafficGenerator", untouchable)
    argv = ["run", "provider-outage", "--model", "opus", "--evidence-dir", str(tmp_path / "evidence")]
    assert fault_runs.main(argv) == 2
    assert "Not started, and nothing was touched" in capsys.readouterr().err
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("argv", [["--steps", "1"], ["--seconds", "0"], ["--rate", "0"]])
def test_bad_budgets_or_traffic_settings_are_refused_before_anything(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(fault_runs, "preflight", lambda model: pytest.fail("pre-flight ran"))
    assert fault_runs.main(["run", "poison-message", *argv, "--evidence-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []


# The command, end to end, offline

WEEK3 = ["--no-actions"]  # week 3's run: no write action offered, the loop only diagnoses


def patch_command(monkeypatch, world, script=None):
    """The command with this made-up stack and traffic, and Klaxon's real loop: scripted replies, and Klaxon's real
    MCP server code serving made-up tool results, in this process."""
    monkeypatch.setattr(fault_runs, "preflight", lambda model: Path("/made-up/claude"))
    monkeypatch.setattr(fault_runs.faults, "get", lambda name: world.fault(name))
    monkeypatch.setattr(fault_runs.faults, "LocalStack", lambda: None)
    monkeypatch.setattr(fault_runs.alerts, "Alerting", world.alerting)
    monkeypatch.setattr(fault_runs, "Stack", lambda local, alerting: world.stack())
    monkeypatch.setattr(fault_runs, "_provenance", lambda command: {"rerun_command": command, "git_commit": "made-up"})
    script = script or [tool("query_metrics", query="sum(rate(klaxon_provider_attempts_total[1m]))"), DIAGNOSE_OUTAGE]
    given = []

    def investigate_live(alert, budgets, folder, header, model_id, binary, actions=()):
        given.append((alert, header, model_id))
        return loop.investigate(
            alert, ScriptedModel(script), Served().client, folder / loop.TRACE, budgets, actions=actions
        )

    monkeypatch.setattr(fault_runs.loop, "investigate_live", investigate_live)
    return given


def patch_quiet(monkeypatch, world):
    monkeypatch.setattr(fault_runs.alerts, "Alerting", world.alerting)
    monkeypatch.setattr(fault_runs, "_provenance", lambda command: {"rerun_command": command})
    real = fault_runs.quiet

    def quiet(alerting, settings, minutes):
        return real(alerting, settings, minutes, lambda s: FakeTraffic(world, s), world.sleep, world.clock)

    monkeypatch.setattr(fault_runs, "quiet", quiet)


@pytest.fixture
def offline(monkeypatch):
    world = World()
    return world, patch_command(monkeypatch, world)


DIAGNOSE_OUTAGE = json.dumps(
    {
        "action": "diagnose",
        "why": "made up for the test",
        "diagnosis": {
            "hypothesis": "The payment provider times out; the circuit breaker opened, and payments-api fails fast.",
            "evidence": [{"call_id": "c1", "shows": "made up"}],
            "confidence": 0.7,
            "proposed_fix": {"kind": "none", "target": "fake-provider", "summary": "Nothing to change on our side."},
            "blast_radius": "made up",
            "rollback_plan": "made up",
        },
    }
)


def test_the_command_runs_one_fault_and_saves_everything_in_one_folder(offline, tmp_path, capsys):
    world, given = offline
    assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 0
    [folder] = tmp_path.iterdir()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_fault-run_provider-outage", folder.name)
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["label"] == fault_runs.label(fault_runs.Record(**{k: saved[k] for k in RECORD_FIELDS}))
    assert saved["printed_output"][0] == "Klaxon week 3: a run of the first loop on a fault: provider-outage"
    for claim in (
        "provider-outage, was injected on purpose",
        "The alert was raised",
        "ran live on it",
        "not a simulation",
        "by a rule",
    ):
        assert claim in saved["label"], claim
    assert "what" not in saved  # the loop's own files say what a loop run is; run.json says what this run did
    assert saved["fault"]["cause"] == faults.FAULTS["provider-outage"].cause
    assert saved["alert"]["name"] == "PaymentsLatencyHigh" and saved["stopped"] == "the loop stopped: diagnosed"
    assert saved["comparison"]["by"] == grading.BY[2] and saved["comparison"]["verdict"] == "names the cause"
    assert saved["rerun_command"].startswith("python -m klaxon.fault_runs run provider-outage")
    [(alert, header, model_id)] = given
    assert model_id == "claude-opus-5-5" and header["alert"]["summary"] == alert.summary
    assert header["fault_name"] == "provider-outage" and header["budgets"]["steps"] == 12
    assert read_trace(folder / loop.TRACE)[-1]["reason"] == "diagnosed"
    out = capsys.readouterr().out
    assert "Compared by a rule (klaxon.grading, rubric version 2" in out and "verdict: NAMES THE CAUSE" in out
    assert fault_runs.main(["grade", str(folder)]) == 0
    assert "verdict: NAMES THE CAUSE" in capsys.readouterr().out


def test_a_run_that_fails_midway_still_saves_its_record_and_says_so(offline, tmp_path, monkeypatch, capsys):
    world, _ = offline
    world.clear_fails = True
    assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 2
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["stopped"] == "error: RuntimeError: docker did not answer" and saved["cleared"] is None
    assert "Clearing it failed, so it may still be in place." in saved["label"]  # the re-check: untested before
    assert "THE FAULT MAY STILL BE IN PLACE: make fault-clear FAULT=provider-outage" in capsys.readouterr().err


def test_an_interrupted_run_saves_its_record(offline, tmp_path, monkeypatch):
    world, _ = offline
    monkeypatch.setattr(
        fault_runs.loop, "investigate_live", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 130
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["stopped"] == "interrupted" and saved["cleared"] == {"made_up": "cleared"}


def test_a_run_whose_alert_never_fired_exits_1(offline, tmp_path):
    world, given = offline
    world.sick = {}
    assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 1
    assert given == []


def test_the_quiet_command_saves_its_evidence(monkeypatch, tmp_path, capsys):
    patch_quiet(monkeypatch, World())
    assert fault_runs.main(["quiet", "--minutes", "1", "--evidence-dir", str(tmp_path)]) == 0
    [path] = tmp_path.iterdir()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_alerts-under-traffic_rate3_seed7\.json", path.name)
    saved = json.loads(path.read_text())
    assert saved["label"] == fault_runs.QUIET_LABEL and saved["alerts_fired"] == 0
    assert "5 evaluations: 0 with an alert" in capsys.readouterr().out


def test_grading_a_folder_without_a_run_is_refused(tmp_path, capsys):
    assert fault_runs.main(["grade", str(tmp_path)]) == 2
    assert "Cannot grade" in capsys.readouterr().err


# After the independent review (DECISIONS.md lists its findings by number)

RECORD_FIELDS = list(fault_runs.Record.__dataclass_fields__)
NEVER_CLAIMED = (
    "injected on purpose",
    "alert was raised",
    "ran live",
    "started live",
    "recorded response",
    "by a rule",
)


@pytest.mark.parametrize(
    "world, stopped, said",
    [
        (World(before=SLOW), "not quiet before the fault", ()),
        (World(sick={}), "no alert fired within 180 s", ("injected on purpose",)),
    ],
    ids=["not quiet", "no alert"],
)
def test_a_run_that_stopped_early_claims_nothing_it_did_not_do(world, stopped, said, tmp_path, monkeypatch):
    # Finding 1: the label was fixed text, and claimed a fault, an alert, a live loop and a comparison every time.
    patch_command(monkeypatch, world)
    assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 1
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["stopped"].startswith(stopped) and stopped in saved["label"]
    assert [claim for claim in NEVER_CLAIMED if claim in saved["label"]] == list(said)
    assert saved["printed_output"][1] == saved["label"]
    assert ("No alert fired" if said else "No fault was injected") in saved["label"]


def test_a_loop_that_did_not_finish_is_said_to_have_started_only(tmp_path, monkeypatch):
    world = World()
    patch_command(monkeypatch, world)
    monkeypatch.setattr(
        fault_runs.loop, "investigate_live", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 130
    [folder] = tmp_path.iterdir()
    label = json.loads((folder / fault_runs.RUN_FILE).read_text())["label"]
    assert "Klaxon's loop started live on it, through Claude Code on Aravind's seat, and did not finish" in label
    assert "recorded response" not in label and "by a rule" not in label


def test_a_loop_that_stopped_without_a_diagnosis_is_not_said_to_be_compared(tmp_path, monkeypatch):
    # The re-check of finding 1: this sentence had no test.
    world = World()
    patch_command(monkeypatch, world, script=[tool("service_health"), tool("service_health")])
    argv = ["run", "provider-outage", *WEEK3, "--steps", "2", "--tool-calls", "1", "--evidence-dir", str(tmp_path)]
    assert fault_runs.main(argv) == 1
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["comparison"]["verdict"] == "no diagnosis" and saved["loop"]["reason"] == "tool_call_budget"
    assert "stopped on tool_call_budget after 2 steps" in saved["label"] and "by a rule" not in saved["label"]


def test_a_run_whose_traffic_never_began_claims_nothing(tmp_path, monkeypatch):
    world = World()
    world.traffic_fails = True
    patch_command(monkeypatch, world)
    assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 2
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["traffic"] is None and "stopped before its synthetic traffic began" in saved["label"]
    assert "with synthetic traffic" not in saved["label"] and world.calls == []


def test_a_failed_injection_is_not_called_a_fault_injected(tmp_path, monkeypatch):
    world = World()
    world.inject_fails = True
    patch_command(monkeypatch, world)
    assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 2
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["injected"] is None and saved["cleared"] == {"made_up": "cleared"}
    assert "began on purpose and failed" in saved["label"] and "was injected on purpose" not in saved["label"]
    assert "No alert fired" not in saved["label"]
    assert any(line.startswith("  an injection began at t=") for line in saved["printed_output"])


def test_one_blip_does_not_fail_a_quiet_run(tmp_path, monkeypatch, capsys):
    # The re-check of finding 3: the rules let one blip pass, so a quiet run counts it and does not fail on it.
    world = World()
    real = world.prometheus
    asked = []

    def one_slow_minute(query):
        if query == alerts.QUERIES["p95_seconds"]:
            asked.append(query)
            if len(asked) == 2:
                return SLOW["p95_seconds"]
        return real(query)

    world.prometheus = one_slow_minute
    patch_quiet(monkeypatch, world)
    assert fault_runs.main(["quiet", "--minutes", "1", "--evidence-dir", str(tmp_path)]) == 0
    [path] = tmp_path.iterdir()
    saved = json.loads(path.read_text())
    assert (saved["alerts_fired"], saved["conditions_held"], saved["evaluations_blind"]) == (0, 1, 0)


def test_each_evaluation_says_when_it_began_and_how_long_it_took():
    # Finding 4: times were taken at the end of an evaluation, and the alert's at the end of the one that fired.
    world = World(sick={"lag_by_group": [({"consumer": "ledger"}, 271.0)]})
    world.money_takes = 15.0
    record = run(world)
    during = [e for e in record.evaluations if e["phase"] == "during"]
    assert [(e["began"], e["took"]) for e in during] == [(120.0, 15.0), (150.0, 15.0)]
    assert record.timings["alert_evaluation_began"] == 150.0 and record.alert["name"] == "ConsumerLagHigh"


def test_after_clearing_the_rules_read_the_stack_afresh():
    # Finding 7: the counts carried over from the fault, so one breach after clearing showed as firing.
    world = World()
    world.lingers = True
    record = run(world)
    [after] = [e for e in record.evaluations if e["phase"] == "after"]
    [latency] = [c for c in after["checks"] if c["rule"] == "PaymentsLatencyHigh"]
    assert (latency["breached"], latency["in_a_row"], latency["firing"]) == (True, 1, False)


@pytest.mark.parametrize(
    "before, money_before",
    [({"p95_seconds": [], "requests_per_second": []}, PASSED), ({}, {"outcome": "could not settle"})],
    ids=["no data", "the money check never settles"],
)
def test_a_quiet_run_that_could_not_see_is_not_quiet(before, money_before, tmp_path, monkeypatch, capsys):
    # Finding 3: alerts and problems were counted, but a rule with no data, or a check that never settled, was neither.
    world = World(before=before, money_before=money_before)
    found = fault_runs.quiet(
        world.alerting(), Settings(run_id="r1"), 1.0, lambda s: FakeTraffic(world, s), world.sleep, world.clock
    )
    assert found["alerts_fired"] == 0 and found["evaluations_blind"] == len(found["evaluations"]) == 5
    assert all(e["blind"] for e in found["evaluations"])
    patch_quiet(monkeypatch, World(before=before, money_before=money_before))
    assert fault_runs.main(["quiet", "--minutes", "1", "--evidence-dir", str(tmp_path)]) == 1
    assert "5 blind" in capsys.readouterr().out


def test_the_make_targets_pass_their_values_through_the_environment():
    # Finding 9: the four new targets were not in item 3's quoting test.
    for target, value, name in [
        ("investigate-fault", "FAULT=x' ; echo pwned", '"$FAULT"'),
        ("investigate-fault", "NO_ACTIONS=1' ; echo pwned", "${NO_ACTIONS:+--no-actions}"),  # week 4: never pasted
        ("grade-run", "RUN=x' ; echo pwned", '"$RUN"'),
        ("alerts", "WATCH=1' ; echo pwned", '"$WATCH"'),
        ("alerts-under-traffic", "MINUTES=1' ; echo pwned", '"$MINUTES"'),
    ]:
        shown = subprocess.run(["make", "-n", target, value], cwd=loop.REPO, capture_output=True, text=True, check=True)
        assert "pwned" not in shown.stdout and name in shown.stdout, target


# Nothing the model is given names the fault

GIVEAWAYS = (
    "duplicate-delivery",
    "poison",
    "provider-outage",
    "outage",
    "naive",
    "dedup",
    "circuit breaker",
    "malformed",
    "unreadable",
    "decimal",
    "12.50",
    r"\bhangs?\b",
    "=retry",
)


def test_nothing_the_model_is_given_names_a_fault_or_its_cause():
    specs = [mc.ToolSpec(t.name, t.title, t.description, t.input_schema, READ_ONLY) for t in make_tools().values()]
    texts = [loop.system_prompt(specs)]
    for evaluation in (
        _fired(World(sick=SLOW)),
        _fired(World(sick={"errors_per_second": [({}, 2.0)], "requests_per_second": [({}, 2.5)]})),
        _fired(World(sick={"lag_by_group": [({"consumer": "ledger"}, 190.0)]})),
        _fired(World(sick={}, money_sick=FAILED)),
    ):
        texts.append(loop.first_turn(evaluation.alert, loop.Budgets()))
    for text in texts:
        assert not [word for word in GIVEAWAYS if re.search(word, text, re.IGNORECASE)], text[:200]


def _fired(world):
    world.fault_on = True
    alerting = world.alerting()
    for _ in range(alerts.IN_A_ROW):
        evaluation = alerting.evaluate()
    assert evaluation.alert is not None
    return evaluation


# The live runs on the faults, replayed: regression tests from evidence (STATUS, week 3 item 4)

FAULT_RUNS = {
    "duplicate-delivery": "2026-10-19T022933Z_fault-run_duplicate-delivery",
    "poison-message": "2026-10-19T023414Z_fault-run_poison-message",
    "provider-outage": "2026-10-19T024001Z_fault-run_provider-outage",
}


@pytest.mark.parametrize("fault", sorted(FAULT_RUNS))
def test_each_live_fault_run_replays_exactly_and_grades_the_same(fault, tmp_path):
    """Replies received from Claude on 2026-10-19 UTC (claude-opus-5-5) and the tool results of that run, served
    again: the loop must make the same decisions. Nothing is called. A change to the system prompt means no recorded
    reply can match it: then this skips, saying why. A change to the grading rule shows here as a failure."""
    folder = loop.REPO / "evidence" / "week3" / FAULT_RUNS[fault]
    recorded = read_trace(folder / loop.TRACE)
    specs = mc.ReplayTools(folder / loop.TOOL_RESULTS).open()
    if text_sha256(loop.system_prompt(loop.allowed_tools(specs))) != recorded[0]["system"]["sha256"]:
        pytest.skip("the system prompt changed since the fault runs: record new runs to replay")
    result, whole, differences = loop.replay(folder, tmp_path / "replay.jsonl")
    assert (whole, differences) == (True, [])
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert result.reason == "diagnosed" and (result.steps, result.tool_calls) == (
        saved["loop"]["steps"],
        saved["loop"]["tool_calls"],
    )
    assert recorded[0]["source"] == "claude-code" and recorded[0]["model"] == "claude-opus-5-5"
    # The loop got exactly the alert the rules raised, at the first evaluation that fired after the injection.
    fired = [e for e in saved["evaluations"] if e["phase"] == "during" and e["alert"] is not None]
    assert recorded[0]["extra"]["alert"] == saved["alert"] == fired[0]["alert"]
    # The rule, run again on the diagnosis the trace kept, says what the run recorded: the same rubric, version 1.
    # Saved before comparisons recorded their version, so graded again with version 1 (week 4, item 3).
    assert grading.version_of(saved["comparison"]) == 1
    again = grading.compare(fault, recorded[-1]["diagnosis"], version=1)
    kept = ("verdict", "elements", "proposed_fix", "fix_fits_the_remedy", "hypothesis", "recorded_cause")
    assert {key: again[key] for key in kept} == {key: saved["comparison"][key] for key in kept}
    assert again["rubric"]["version"] == 1 and "before the live runs" in saved["comparison"]["by"]
    assert saved["fault"]["name"] == fault and saved["cleared"] is not None


@pytest.mark.parametrize(
    "saved, trace, why",
    [
        ("[]", '{"step": 0}\n', "ValueError: its run.json is not an object"),
        ('{"fault": {"name": "provider-outage"}, "comparison": "v1"}', '{"step": 0}\n', "is not an object"),
        ('{"fault": {"name": "provider-outage"}}', "", "IndexError"),  # an empty trace
    ],
)
def test_a_saved_run_that_cannot_be_read_is_not_graded(tmp_path, capsys, saved, trace, why):
    """The review's finding 8: a run.json in a shape a run never writes is refused, without a traceback."""
    (tmp_path / fault_runs.RUN_FILE).write_text(saved)
    (tmp_path / loop.TRACE).write_text(trace)
    assert fault_runs.main(["grade", str(tmp_path)]) == 2
    said = capsys.readouterr().err
    assert said.startswith("Cannot grade") and why in said


def test_grading_a_saved_run_again_uses_the_version_that_graded_it(capsys):
    """make grade-run on the week 3 run of fault 3 (week 4, item 3): version 1 graded it, and grades it again the same
    way. Version 2 reads the same diagnosis as only partly right: with the breaker, it misses how the API fails."""
    folder = loop.REPO / "evidence" / "week3" / FAULT_RUNS["provider-outage"]
    assert fault_runs.main(["grade", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "rubric version 1, committed in 95a64ab" in out and out.rstrip().endswith("verdict: NAMES THE CAUSE")
    assert "has no circuit breaker" in out  # the cause version 1 recorded
    diagnosis = read_trace(folder / loop.TRACE)[-1]["diagnosis"]
    assert grading.compare("provider-outage", diagnosis)["verdict"] == "partly"


# Week 4, item 2: gated runs. Klaxon acts on its diagnosis through the gate before the harness clears the fault.
# The gate's answers, the verifications and the deploy log are made up here; the command's end-to-end test below runs
# the real gate, answered by a scripted approver (ScriptedTerminal, in make approve's real code): no person.

LEDGER = "ledger-consumer"
CONTAINER = "0" * 64
ROLLBACK = FixAction("rollback_deploy", {"service": LEDGER})


def deploy_line(seq, by, settings):
    return Deploy(seq, "2026-10-20T00:00:00Z", LEDGER, by, f"{seq:064x}", settings, CONTAINER, None)


def made_up_verification(verdict="passed"):
    """What verify.verify returns, made up: no stack is read."""
    reading = verify.Reading("2026-10-20T00:05:00Z", "passed", 1_792_400_000)
    health = verify.Health(verdict != "failed", None if verdict != "failed" else "made up", ())
    cause = {"passed": True, "failed": False, "could not verify": None}[verdict]
    money = verify.Money(cause, cause, {verify.BEFORE: [], verify.SURFACED: [], verify.NEW: []})
    return verify.Verification(verdict, health, reading, reading, money)


class Klaxon:
    """Klaxon's part after its diagnosis, made up: the gate answers `result` at once (a scripted approver, not a
    person), the verification says `verdict`, and the deploy log is a list. A done rollback removes the cause."""

    def __init__(self, world, result="done", verdict="passed", deploys_fail=False):
        self.world, self.result, self.verdict, self.deploys_fail = world, result, verdict, deploys_fail
        self.log = [deploy_line(1, "make up, before the run", {})]
        self.rules_read = []

    def parts(self):
        return fault_runs.Gated(
            self.read_money, self.act, self.decided, self.verify_action, self.propose_undo, self.deploys
        )

    def read_money(self):
        self.world.calls.append("money at the request")
        return verify.Reading("2026-10-20T00:03:00Z", "failed", 1_792_399_900, money_off_cents=46_100)

    def act(self, alert, diagnosis, before_asking):
        self.world.calls.append("gate")
        if self.result in ("no action", "refused"):  # as the real gate: no plan, so no request and nobody asked
            return gate.Outcome(self.result, "made up")
        before_asking()  # as the real gate: once a plan is made, right before its request
        self.world.now += 45.0  # the time it took to answer
        if self.result != "done":
            return gate.Outcome(self.result, "made up", "r-1234")
        self.log.append(
            deploy_line(len(self.log) + 1, "Klaxon, approved request r-1234", {"LEDGER_HANDLER": "idempotent"})
        )
        self.world.fault_on = False  # the rollback removed the cause
        return gate.Outcome("done", "redeploy ledger-consumer: made up", "r-1234", {"seq": len(self.log)})

    def decided(self):
        return {"decision": "approved", "how": "typed at a terminal", "at": "x", "written_by_make_approve": True}

    def verify_action(self, rules):
        self.world.calls.append("verify")
        self.rules_read += [rules(), rules()]
        return made_up_verification(self.verdict)

    def propose_undo(self, outcome, found):
        self.world.calls.append("undo")
        return gate.Outcome("refused", f"Klaxon will not undo it: made up. {gate.PERSONS_CALL}", outcome.request_id)

    def deploys(self):
        if self.deploys_fail:
            raise DeployError("the deploy log has a line it cannot read (line 1)")
        return list(self.log)


class DeployFault(World):
    """A made-up stack whose fault is a bad deploy of the ledger consumer: injecting and clearing it write the
    made-up deploy log, as klaxon.faults does; a clearing that finds the good settings running changes nothing."""

    def __init__(self, **kwargs):
        super().__init__(sick={}, money_sick=FAILED, **kwargs)
        self.klaxon = Klaxon(self)

    def inject(self, local):
        self.klaxon.log.append(deploy_line(len(self.klaxon.log) + 1, "klaxon.faults", {"LEDGER_HANDLER": "naive"}))
        return super().inject(local)

    def clear(self, local):
        if self.fault_on:
            self.klaxon.log.append(
                deploy_line(len(self.klaxon.log) + 1, "klaxon.faults", {"LEDGER_HANDLER": "idempotent"})
            )
        super().clear(local)
        return {"redeployed": LEDGER, "LEDGER_HANDLER": "idempotent"}

    def fault(self, name="duplicate-delivery"):
        return super().fault(name)


def gated_run(world, diagnosis=DIAGNOSIS, fix_action=True, **kwargs):
    named = (
        replace(diagnosis, fix_action=FixAction("rollback_deploy", {"service": LEDGER})) if fix_action else diagnosis
    )

    def investigate(alert):
        world.calls.append(("investigate", alert))
        world.now += 60.0
        return loop.RunResult("diagnosed", "confidence 0.7", named, 5, 4, 60.0, Path("trace.jsonl"))

    return run(world, investigate=investigate, gated=world.klaxon.parts(), **kwargs)


def test_a_gated_run_lets_klaxon_act_and_verify_before_the_harness_clears_the_fault():
    world = DeployFault()
    record = gated_run(world)
    assert calls(world) == [
        "traffic on",
        "inject",
        "investigate",
        "gate",
        "money at the request",  # once the gate has a plan, right before its request
        "verify",
        "clear",
        "traffic closed",
        "traffic joined",
    ]
    assert record.gated and record.gate["result"] == "done" and record.verification["verdict"] == "passed"
    assert record.undo is None
    assert [e["phase"] for e in record.evaluations] == ["before", "before", "during", "verify", "verify", "after"]
    assert record.removed_by == (
        "Klaxon: its rollback (Klaxon, approved request r-1234) removed the cause, and the harness's clearing then"
        " found the good settings running, and changed nothing"
    )
    assert [(line["seq"], line["by"]) for line in record.deploys] == [
        (2, "klaxon.faults"),
        (3, "Klaxon, approved request r-1234"),
    ]
    timings = record.timings
    assert timings["loop_ended_at"] <= timings["gate_began_at"] < timings["gate_ended_at"] <= timings["verify_began_at"]
    assert timings["verify_ended_at"] <= timings["cleared_at"]


def test_verification_reads_the_rules_afresh_after_the_action():
    world = DeployFault()
    act = world.klaxon.act

    def leaves_old_damage(alert, diagnosis, before_asking):
        outcome = act(alert, diagnosis, before_asking)
        world.money_before = FAILED  # the rollback removed the cause; the money booked twice stays wrong
        return outcome

    world.klaxon.act = leaves_old_damage
    record = gated_run(world)
    first, second = world.klaxon.rules_read
    [money] = [check for check in first.checks if check.rule == "MoneyCheckFailed"]
    assert money.breached and money.in_a_row == 1  # counted afresh: nothing carried over from the alert
    assert [e["phase"] for e in record.evaluations].count("verify") == 2


def test_the_damage_is_recorded_at_the_alert_the_request_the_action_and_after_it():
    world = DeployFault()
    record = gated_run(world)
    assert list(record.damage) == ["at_alert", "at_request", "at_action", "after_action"]
    alert = record.damage["at_alert"]
    assert alert["source"] == "the alert's own evaluation of the rules" and alert["money_check"] == FAILED
    assert alert["began"] == record.timings["alert_evaluation_began"]
    assert alert["lag"] == {"ledger": 0.0, "payment-confirmer": 1.0}
    assert (
        record.damage["at_request"]["outcome"] == "failed" and record.damage["at_request"]["money_off_cents"] == 46_100
    )
    assert record.damage["at_action"]["outcome"] == "passed"


@pytest.mark.parametrize("result", ["denied", "expired", "refused", "not run", "failed"])
def test_an_action_that_did_not_run_is_not_verified_and_the_harness_removes_the_cause(result):
    world = DeployFault()
    world.klaxon.result = result
    record = gated_run(world)
    assert "verify" not in calls(world) and record.verification is None and record.undo is None
    assert calls(world).index("gate") < calls(world).index("clear")
    assert record.gate["result"] == result and record.removed_by == "the harness's clearing (klaxon.faults)"
    # the review's L1: a plan the gate refused makes no request, so there is no damage "at the request"
    assert list(record.damage) == (["at_alert"] if result == "refused" else ["at_alert", "at_request"])


def test_a_diagnosis_that_names_no_action_reads_no_money_and_the_gate_asks_nobody():
    world = DeployFault()
    world.klaxon.result = "no action"
    record = gated_run(world, fix_action=False)
    assert "money at the request" not in calls(world) and "gate" in calls(world)
    assert record.gate["decided"] is None and list(record.damage) == ["at_alert"]
    assert "Its diagnosis named no action, so nobody was asked to approve one." in fault_runs.label(record)


def test_a_failed_verification_proposes_the_undo_before_the_fault_is_cleared():
    world = DeployFault()
    world.klaxon.verdict = "failed"
    record = gated_run(world)
    assert calls(world)[calls(world).index("verify") :][:3] == ["verify", "undo", "clear"]
    assert record.undo["result"] == "refused" and gate.PERSONS_CALL in record.undo["detail"]
    said = fault_runs.label(record)
    assert "The verification failed, so Klaxon proposed undoing its action, and the gate refused it" in said


def test_a_verification_that_could_not_verify_proposes_no_undo():
    world = DeployFault()
    world.klaxon.verdict = "could not verify"
    record = gated_run(world)
    assert "undo" not in calls(world) and record.undo is None


@pytest.mark.parametrize("where", ["act", "verify_action"])
def test_the_fault_is_cleared_and_the_traffic_stopped_when_the_gate_or_verification_breaks(where):
    world = DeployFault()
    parts = world.klaxon.parts()

    def breaks(*args):
        world.calls.append(where)
        raise KeyboardInterrupt

    parts = replace(parts, **{where: breaks})

    def investigate(alert):
        world.calls.append(("investigate", alert))
        return loop.RunResult("diagnosed", "x", replace(DIAGNOSIS, fix_action=ROLLBACK), 5, 4, 60.0, Path("t"))

    record = record_for(world.fault())
    with pytest.raises(KeyboardInterrupt):
        fault_runs.run(world.fault(), record, world.stack(), Settings(run_id="r1"), investigate, gated=parts)
    assert calls(world)[-3:] == ["clear", "traffic closed", "traffic joined"]
    # A verification that broke came after Klaxon's rollback ran: the rollback removed the cause all the same.
    assert record.removed_by.startswith("the harness's clearing" if where == "act" else "Klaxon: its rollback")
    record.stopped = "interrupted"
    said = fault_runs.label(record)
    if where == "act":
        assert "Klaxon took its diagnosis to the approval gate, and the run stopped there: interrupted." in said
    else:
        assert "Its verification began and did not finish: interrupted." in said


def test_who_removed_the_cause_when_klaxon_rolled_back_the_wrong_service():
    world = DeployFault()
    act = world.klaxon.act

    def wrong_service(alert, diagnosis, before_asking):
        outcome = act(alert, diagnosis, before_asking)
        world.fault_on = True  # its rollback did not touch the ledger consumer's bad setting
        return outcome

    world.klaxon.act = wrong_service
    record = gated_run(world)
    assert record.removed_by == (
        "the harness's clearing (klaxon.faults), although Klaxon had rolled back: the clearing still found something"
        " to change"
    )


def test_a_fault_whose_cause_is_not_a_deploy_is_removed_by_the_harness_whatever_klaxon_did():
    world = DeployFault()
    world.clear = lambda local: (world.calls.append("clear"), {"provider": "answering again"})[1]
    record = gated_run(world)
    assert record.removed_by == (
        "the harness's clearing: this fault's cause is not a deploy, so Klaxon's rollback did not remove it"
    )


def test_a_deploy_log_that_cannot_be_read_never_stops_the_clearing_and_is_said():
    world = DeployFault()
    world.klaxon.deploys_fail = True
    record = gated_run(world)
    assert "clear" in calls(world) and record.cleared is not None
    assert record.removed_by.startswith("not known: the deploy log could not be read")


def gated_record(world, investigate=None):
    """A gated run's own record, kept even when the run raises: run() fills it in as it goes."""
    record = record_for(world.fault())

    def diagnosed_with_rollback(alert):
        return loop.RunResult("diagnosed", "x", replace(DIAGNOSIS, fix_action=ROLLBACK), 1, 1, 1.0, Path("t"))

    try:
        fault_runs.run(
            world.fault(),
            record,
            world.stack(),
            Settings(run_id="r1"),
            investigate or diagnosed_with_rollback,
            gated=world.klaxon.parts(),
        )
    except RuntimeError:
        pass
    return record


def test_a_clearing_that_failed_is_said_as_the_cause_not_known_removed():
    world = DeployFault()
    world.klaxon.result, world.clear_fails = "denied", True
    record = gated_record(world)
    assert record.removed_by == "not known: the harness's clearing failed, so the fault may still be in place"
    # The review's M2: "not known" is never said as if it were someone who removed the cause.
    said = fault_runs.label(record)
    assert (
        "Who removed the fault's cause is not known: the harness's clearing failed, so the fault may still be in place."
    ) in said
    assert "removed by not known" not in said and "removed by not known" not in fault_runs.render(record)


def test_a_deploy_log_unreadable_only_at_the_clearing_never_lets_klaxon_take_the_clearings_work():
    # The review's L2: with the clearing's mark unknown, a clearing that deployed could be missed, and the record
    # would say Klaxon removed the cause. Here Klaxon's rollback missed the bad setting, and the clearing deployed.
    world = DeployFault()
    act, reads = world.klaxon.act, []

    def wrong_service(alert, diagnosis, before_asking):
        outcome = act(alert, diagnosis, before_asking)
        world.fault_on = True
        return outcome

    def unreadable_at_the_clearing():
        reads.append(1)
        if len(reads) == 2:  # the first read is the mark before the fault, the second the clearing's
            raise DeployError("the deploy log has a line it cannot read (line 4)")
        return list(world.klaxon.log)

    world.klaxon.act, world.klaxon.deploys = wrong_service, unreadable_at_the_clearing
    record = gated_record(world)
    assert record.removed_by == "not known: the deploy log could not be read"
    assert [line["by"] for line in record.deploys] == [
        "klaxon.faults",
        "Klaxon, approved request r-1234",
        "klaxon.faults",
    ]


def test_a_gated_run_that_stopped_before_its_fault_says_nothing_of_who_removed_it():
    # The review's L2: no fault was injected, so there is no cause to remove, and no deploy log is read for one.
    world = DeployFault(before=SLOW)
    record = gated_record(world)
    assert "inject" not in calls(world)
    assert record.removed_by is None and record.deploys is None and record.damage == {}
    assert "removed" not in fault_runs.label(record)


def test_a_gated_run_whose_traffic_never_began_is_still_said_as_gated():
    # A nit from the review: record.gated was set after the traffic started.
    world = DeployFault()
    world.traffic_fails = True
    record = record_for(world.fault())
    with pytest.raises(httpx.ConnectError):
        fault_runs.run(
            world.fault(), record, world.stack(), Settings(run_id="r1"), diagnosed(world), gated=world.klaxon.parts()
        )
    assert record.gated and fault_runs.render(record).startswith("Klaxon week 4: a gated run on a fault")


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGHUP])
def test_a_stop_signal_during_a_run_clears_the_fault_as_ctrl_c_does(stop, monkeypatch, tmp_path):
    # The review's M1: Python's own default for these signals ends the process at once, with no finally run, so a
    # stopped background run would have left its fault in place, and no evidence.
    world = World()
    patch_command(monkeypatch, world)

    def stopped(*args, **kwargs):
        world.calls.append("investigate")
        os.kill(os.getpid(), stop)
        raise AssertionError("the stop signal did not interrupt the run")

    monkeypatch.setattr(fault_runs.loop, "investigate_live", stopped)
    inherited = signal.signal(stop, signal.SIG_DFL)  # as a terminal leaves it (under nohup, SIGHUP is ignored: below)
    try:
        assert fault_runs.main(["run", "provider-outage", *WEEK3, "--evidence-dir", str(tmp_path)]) == 130
        assert signal.getsignal(stop) == signal.SIG_DFL  # put back once the run is over
    finally:
        signal.signal(stop, inherited)
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["stopped"] == f"interrupted ({stop.name})" and saved["cleared"] == {"made_up": "cleared"}
    assert calls(world)[-3:] == ["clear", "traffic closed", "traffic joined"]


def test_a_stop_signal_ignored_on_purpose_stays_ignored():
    # The re-check's A: a run started under nohup ignores SIGHUP to outlive its terminal, and must keep doing so.
    inherited = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with fault_runs.stops_as_interrupts():
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN, None)
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, inherited)


def test_a_stop_signal_during_a_quiet_run_still_stops_the_traffic(monkeypatch, tmp_path):
    world = World()
    patch_quiet(monkeypatch, world)
    real = world.prometheus

    def stopped(query):
        os.kill(os.getpid(), signal.SIGTERM)
        return real(query)

    world.prometheus = stopped
    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        fault_runs.main(["quiet", "--minutes", "1", "--evidence-dir", str(tmp_path)])
    assert calls(world)[-2:] == ["traffic closed", "traffic joined"]


@pytest.mark.parametrize(
    "result, decided, said",
    [
        (
            "done",
            True,
            "the request was approved with make approve (the request's id typed at a terminal), and the action ran",
        ),
        (
            "done",
            False,
            "the request was approved by a decision that no line of make approve names, and the action ran",
        ),
        ("expired", None, "nobody answered within 10 minutes, which counts as denied, and nothing ran"),
        ("denied", True, "the request was denied (made up), and nothing ran"),
        ("refused", None, "Its diagnosis named an action that code refused to plan, so nobody was asked: made up."),
    ],
)
def test_a_gated_runs_label_says_how_the_request_was_answered(result, decided, said):
    world = DeployFault()
    world.klaxon.result = result
    if decided is not None:
        world.klaxon.decided = lambda: {"decision": "x", "how": "x", "at": "x", "written_by_make_approve": decided}
    record = gated_run(world)
    label = fault_runs.label(record)
    assert said in label
    assert "offered one write action (rollback_deploy), which runs only if a person approves it at the gate" in label
    assert fault_runs.render(record).splitlines()[0] == "Klaxon week 4: a gated run on a fault: duplicate-delivery"


def test_a_week_3_run_says_nothing_of_a_gate():
    world = World()
    record = run(world)
    assert not record.gated and record.gate is None and record.removed_by is None and record.damage == {}
    assert "gate" not in fault_runs.label(record) and "approve" not in fault_runs.label(record)


# The gated command, end to end, offline: the real faults deploy into the made-up docker (tests/fake_docker.py),
# Klaxon's real loop reads scripted replies, the real gate saves its request and waits, a scripted approver answers
# through make approve's real code (gate.approve, with a ScriptedTerminal: no person), the real rollback runs, and the
# real verification reads made-up money checks and the made-up stack's rules.

NAMES_ROLLBACK = json.dumps(
    {
        "action": "diagnose",
        "why": "made up for the test",
        "diagnosis": {
            "hypothesis": "The ledger consumer was redeployed with a handler that books resent webhooks twice.",
            "evidence": [{"call_id": "c1", "shows": "made up"}],
            "confidence": 0.8,
            "proposed_fix": {"kind": "rollback", "target": "ledger-consumer", "summary": "Roll it back."},
            "blast_radius": "made up",
            "rollback_plan": "made up",
            "fix_action": {"name": "rollback_deploy", "arguments": {"service": "ledger-consumer"}},
        },
    }
)


class DockerStack(World):
    """A made-up stack whose ledger consumer runs in the made-up docker: the fault is on while it runs a setting
    other than the good one. The rules read made-up numbers that follow it."""

    def __init__(self):
        super().__init__(sick={}, money_sick=FAILED)
        self.docker = FakeDocker()
        self.local = faults.LocalStack(deployer=self.docker.deployer())

    @property
    def fault_on(self):
        return self.docker.settings(LEDGER) != GOOD[LEDGER]

    @fault_on.setter
    def fault_on(self, value):
        pass  # only deploys change it

    def stack(self):
        return STACK(self.local, self.alerting(), lambda s: FakeTraffic(self, s), self.sleep, self.clock)


def money_checks(world, new_damage=False):
    """Made-up money checks: one event counted twice, from before the action, every time. With new_damage, the
    third check (the one at least a minute after the action) also finds an event made after the action counted
    twice: the cause is still there."""
    old = Event("evt_0001", "payment.succeeded", "pay_0001", 2_500, int(NOW) - 600)
    made = []

    def posted(e):
        return {
            "event_id": e.id,
            "type": e.type,
            "payment_id": e.payment_id,
            "amount_cents": e.amount_cents,
            "times_posted": 2,
            "expected": 1,
            "problem": "counted twice",
            "transaction_ids": [1, 2],
        }

    def check(wait, timeout_seconds):
        made.append(1)
        events = [old]
        if new_damage and len(made) == 3:
            events.append(Event("evt_0099", "payment.succeeded", "pay_0099", 1_250, int(NOW + world.now)))
        evidence = [posted(e) for e in events]
        off = sum(e.amount_cents for e in events)
        report = MoneyReport(False, 0, 0, off, [InvariantResult("posted_exactly_once", False, "made up", evidence)])
        holder = type("Records", (), {"events": events, "payments": [], "refunds": []})()
        return live.LiveCheck(int(NOW + world.now), None, holder, report)

    return check


def patch_gated(monkeypatch, world, script, answer="{id}", new_damage=False, before_answer=None, after_gate=None):
    monkeypatch.setattr(fault_runs, "preflight", lambda model: Path("/made-up/claude"))
    monkeypatch.setattr(fault_runs.faults, "LocalStack", lambda: world.local)
    monkeypatch.setattr(fault_runs.alerts, "Alerting", world.alerting)
    monkeypatch.setattr(fault_runs, "Stack", lambda local, alerting: world.stack())
    monkeypatch.setattr(fault_runs, "_provenance", lambda command: {"rerun_command": command, "git_commit": "made-up"})

    def investigate_live(alert, budgets, folder, header, model_id, binary, actions=()):
        return loop.investigate(
            alert, ScriptedModel(script), Served().client, folder / loop.TRACE, budgets, actions=actions
        )

    monkeypatch.setattr(fault_runs.loop, "investigate_live", investigate_live)
    real = fault_runs.klaxon_parts
    said = []

    def klaxon_parts(folder, deployer):
        parts = real(folder, deployer)
        now = lambda: NOW + world.now  # noqa: E731
        answered = []

        def scripted_approver(seconds):  # the run waits: a scripted approver answers, in make approve's real code
            if not answered and answer is not None:
                answered.append(True)
                if before_answer is not None:
                    before_answer()
                typed = answer.replace("{id}", gate.read_request(folder)["id"])
                gate.approve(folder, lambda: ScriptedTerminal(typed), now)
            world.sleep(seconds)

        check = money_checks(world, new_damage)

        def decided():
            if after_gate is not None:
                after_gate()
            return gate.decided(folder)

        return replace(
            parts,
            decided=decided,
            read_money=lambda: verify.read(check, 15.0, now),
            act=lambda alert, diagnosis, asking: gate.act(
                folder,
                diagnosis,
                deployer,
                asdict(alert),
                sleep=scripted_approver,
                clock=world.clock,
                now=now,
                say=said.append,
                before_asking=asking,
            ),
            verify_action=lambda rules: verify.verify(
                rules, check, lambda: HEALTHY_ROWS, world.sleep, world.clock, now
            ),
        )

    monkeypatch.setattr(fault_runs, "klaxon_parts", klaxon_parts)
    return said


HEALTHY_ROWS = [{"Service": "ledger-consumer", "State": "running", "Health": "healthy", "ExitCode": 0}]


def test_the_gated_command_asks_waits_for_the_approval_acts_verifies_then_clears(monkeypatch, tmp_path, capsys):
    world = DockerStack()
    said = patch_gated(monkeypatch, world, [tool("money_check", wait=True), NAMES_ROLLBACK])
    argv = ["run", "duplicate-delivery", "--evidence-dir", str(tmp_path)]
    assert fault_runs.main(argv) == 0
    [folder] = tmp_path.iterdir()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_gated-run_duplicate-delivery", folder.name)
    assert {path.name for path in folder.iterdir()} >= {
        fault_runs.RUN_FILE,
        gate.REQUEST,
        gate.DECISION,
        gate.LOG,
        loop.TRACE,
    }
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["gate"]["result"] == "done" and saved["gate"]["decided"]["written_by_make_approve"] is True
    assert saved["verification"]["verdict"] == "passed" and saved["undo"] is None
    assert saved["removed_by"].startswith("Klaxon: its rollback (Klaxon, approved request r-")
    assert [line["by"].split(" r-")[0] for line in saved["deploys"]] == ["klaxon.faults", "Klaxon, approved request"]
    assert world.docker.settings(LEDGER) == GOOD[LEDGER]
    assert [json.loads(line)["event"] for line in (folder / gate.LOG).read_text().splitlines()] == [
        "requested",
        "approved",
        "decision taken",
        "running",
        "done",
    ]
    for claim in (
        "duplicate-delivery, was injected on purpose",
        "offered one write action (rollback_deploy)",
        "approved with make approve (the request's id typed at a terminal), and the action ran",
        "Code, not the model, then verified it (klaxon.agent.verify): passed; the cause is removed, and the money is"
        " not right.",
        "The fault's cause was removed by Klaxon",
    ):
        assert claim in saved["label"], claim
    assert list(saved["damage"]) == ["at_alert", "at_request", "at_action", "after_action"]
    assert read_trace(folder / loop.TRACE)[0]["extra"]["loop"]["offered_actions"] == ["rollback_deploy"]
    assert "Klaxon asks to act: request r-" in said[0]
    out = capsys.readouterr().out
    assert out.startswith(f"The run's folder: {folder}")
    assert "The gate: done: redeploy ledger-consumer: LEDGER_HANDLER naive -> idempotent" in out
    assert "  at the action " in out and "1 event counted more than once; our books $25.00 ahead" in out
    assert '  action it names: rollback_deploy {"service": "ledger-consumer"} (it runs only through the gate)' in out


def test_the_gated_command_with_a_denial_runs_nothing_and_the_harness_removes_the_cause(monkeypatch, tmp_path):
    world = DockerStack()
    patch_gated(monkeypatch, world, [tool("money_check", wait=True), NAMES_ROLLBACK], answer="no")
    assert fault_runs.main(["run", "duplicate-delivery", "--evidence-dir", str(tmp_path)]) == 0
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["gate"]["result"] == "denied" and saved["verification"] is None
    assert saved["removed_by"] == "the harness's clearing (klaxon.faults)"
    assert [line["by"] for line in saved["deploys"]] == ["klaxon.faults", "klaxon.faults"]
    assert "the request was denied" in saved["label"] and "nothing ran" in saved["label"]


def test_the_gated_command_with_no_action_named_asks_nobody(monkeypatch, tmp_path):
    world = DockerStack()
    no_action = json.loads(NAMES_ROLLBACK)
    del no_action["diagnosis"]["fix_action"]
    patch_gated(monkeypatch, world, [tool("money_check", wait=True), json.dumps(no_action)])
    assert fault_runs.main(["run", "duplicate-delivery", "--evidence-dir", str(tmp_path)]) == 0
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["gate"]["result"] == "no action" and not (folder / gate.REQUEST).exists()
    assert saved["removed_by"] == "the harness's clearing (klaxon.faults)"


def test_the_week_3_command_offers_no_action_and_leaves_the_week_3_label(monkeypatch, tmp_path):
    world = DockerStack()
    patch_gated(monkeypatch, world, [tool("money_check", wait=True), NAMES_ROLLBACK])
    # with no action offered, a diagnosis that names one is invalid: the loop asks again, and gets the same
    assert fault_runs.main(["run", "duplicate-delivery", *WEEK3, "--evidence-dir", str(tmp_path)]) == 1
    [folder] = tmp_path.iterdir()
    assert folder.name.endswith("_fault-run_duplicate-delivery")
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert not saved["gated"] and saved["gate"] is None and not (folder / gate.REQUEST).exists()
    assert read_trace(folder / loop.TRACE)[0]["extra"]["loop"]["offered_actions"] == []


def test_a_gate_log_that_cannot_be_read_keeps_the_outcome_and_says_who_decided_is_not_known():
    world = DeployFault()

    def unreadable():
        raise gate.GateError("the gate's log cannot be read")

    world.klaxon.decided = unreadable
    record = gated_run(world)
    assert record.gate["result"] == "done" and record.gate["decided"] == {"error": "the gate's log cannot be read"}
    assert "the gate's log cannot be read, so who wrote the decision is not known" in fault_runs.label(record)
    assert "  decision: not known: the gate's log cannot be read" in fault_runs.render(record)


def test_the_gated_command_with_new_damage_after_the_action_fails_and_proposes_the_undo(monkeypatch, tmp_path):
    world = DockerStack()
    patch_gated(monkeypatch, world, [tool("money_check", wait=True), NAMES_ROLLBACK], new_damage=True)
    assert fault_runs.main(["run", "duplicate-delivery", "--evidence-dir", str(tmp_path)]) == 1
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["verification"]["verdict"] == "failed"
    assert saved["verification"]["money"]["judged"][verify.NEW] == ["event evt_0099 posted 2 times"]
    assert saved["undo"]["result"] == "refused"
    assert "would deploy a known-bad setting (LEDGER_HANDLER=naive)" in saved["undo"]["detail"]
    assert gate.PERSONS_CALL in saved["undo"]["detail"]
    events = [json.loads(line)["event"] for line in (folder / gate.LOG).read_text().splitlines()]
    assert events[-2:] == ["undo proposed", "undo refused"]
    assert world.docker.settings(LEDGER) == GOOD[LEDGER]  # the undo ran nothing: Klaxon's rollback stays
    assert "the gate refused it" in saved["label"]


def test_the_gated_command_exits_1_when_an_approved_rollback_fails_in_docker(monkeypatch, tmp_path):
    # The review's L7: an approved action that failed exited 0, as a denial does.
    world = DockerStack()

    def breaks():
        world.docker.up_fails = StackError("the container did not turn healthy")

    def mends():  # once the gate is done: the harness's clearing must still be able to deploy
        world.docker.up_fails = None

    patch_gated(
        monkeypatch, world, [tool("money_check", wait=True), NAMES_ROLLBACK], before_answer=breaks, after_gate=mends
    )
    assert fault_runs.main(["run", "duplicate-delivery", "--evidence-dir", str(tmp_path)]) == 1
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["gate"]["result"] == "failed" and "did not turn healthy" in saved["gate"]["detail"]
    assert saved["verification"] is None and saved["removed_by"] == "the harness's clearing (klaxon.faults)"
    assert "the request was approved with make approve" in saved["label"] and "the action failed" in saved["label"]


def test_the_gated_command_exits_1_when_an_approved_rollback_could_not_run(monkeypatch, tmp_path):
    # The re-check: "not run" had no test. The ledger consumer is recreated by hand between the approval and the run.
    world = DockerStack()
    patch_gated(
        monkeypatch,
        world,
        [tool("money_check", wait=True), NAMES_ROLLBACK],
        before_answer=lambda: world.docker.start(LEDGER, world.docker.settings(LEDGER)),
    )
    assert fault_runs.main(["run", "duplicate-delivery", "--evidence-dir", str(tmp_path)]) == 1
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["gate"]["result"] == "not run" and "changed since the request was made" in saved["gate"]["detail"]
    assert saved["verification"] is None and saved["removed_by"] == "the harness's clearing (klaxon.faults)"


# The gated live runs, replayed: regression tests from evidence (STATUS, week 4 item 2)

GATED_RUNS = {
    "duplicate-delivery, approved": "2026-10-21T090513Z_gated-run_duplicate-delivery",
    "poison-message, unanswered": "2026-10-21T091242Z_gated-run_poison-message",
    "poison-message, approved": "2026-10-21T094537Z_gated-run_poison-message",
}


@pytest.mark.parametrize("name", sorted(GATED_RUNS))
def test_each_gated_live_run_replays_and_its_gate_and_verification_hold(name, tmp_path):
    """Replies received from Claude on 2026-10-21 UTC (claude-opus-5-5), the tool results of that run, its gate's
    request, decision and log, and its verification's money checks, all as the run saved them. Nothing is called: the
    loop must make the same decisions, the gate's files must agree with each other, and the verification's verdict,
    worked out again from the two money checks it kept, must be the one it gave."""
    folder = loop.REPO / "evidence" / "week4" / GATED_RUNS[name]
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    recorded = read_trace(folder / loop.TRACE)
    offered = recorded[0]["extra"]["loop"]["offered_actions"]
    assert offered == ["rollback_deploy"] and recorded[0]["model"] == "claude-opus-5-5"
    specs = loop.allowed_tools(mc.ReplayTools(folder / loop.TOOL_RESULTS).open())
    if text_sha256(loop.system_prompt(specs, fault_runs.OFFERED)) != recorded[0]["system"]["sha256"]:
        pytest.skip("the system prompt changed since the gated runs: record new runs to replay")
    result, whole, differences = loop.replay(folder, tmp_path / "replay.jsonl")
    assert (whole, differences) == (True, [])
    assert result.reason == "diagnosed" and result.diagnosis.fix_action.name == "rollback_deploy"
    # The loop got exactly the alert the rules raised, and the rule grades the kept diagnosis as the run did.
    fired = [e for e in saved["evaluations"] if e["phase"] == "during" and e["alert"] is not None]
    assert recorded[0]["extra"]["alert"] == saved["alert"] == fired[0]["alert"]
    assert saved["comparison"]["rubric"]["version"] == 1  # graded again with it: the version that graded it
    again = grading.compare(saved["fault"]["name"], recorded[-1]["diagnosis"], grading.version_of(saved["comparison"]))
    kept = ("verdict", "elements", "proposed_fix", "fix_fits_the_remedy")
    assert {key: again[key] for key in kept} == {key: saved["comparison"][key] for key in kept}
    # The gate: the request as sealed, a decision naming it whole, and what the run took.
    request, decided = gate.read_request(folder), gate.read_decision(folder)
    assert request["id"] == saved["gate"]["request_id"]
    if saved["gate"]["result"] == "done":
        assert request["plan"]["change"] == saved["gate"]["detail"]  # what ran is what was asked
    assert (decided["request_id"], decided["request_sha256"]) == (request["id"], request["sha256"])
    if saved["gate"]["result"] == "expired":  # nobody answered: the run wrote the denial itself, and nothing ran
        assert decided["decision"] == "expired" and gate.decided(folder) is None
        assert saved["verification"] is None and saved["removed_by"] == "the harness's clearing (klaxon.faults)"
        return
    assert saved["gate"]["result"] == "done" and decided["decision"] == "approved" and decided["how"] == gate.TYPED
    assert gate.decided(folder)["written_by_make_approve"] is True  # a line of make approve names its SHA-256
    # The verification, worked out again from what it kept.
    kept_verification = saved["verification"]
    at_action = verify.Reading.from_dict(kept_verification["at_action"])
    after = verify.Reading.from_dict(kept_verification["after"])
    money = verify.judge(at_action, after)
    assert asdict(money) == kept_verification["money"] and money.cause_removed is True
    health = kept_verification["health"]
    found = verify.Verification(
        kept_verification["verdict"],
        verify.Health(health["healthy"], health["why"], tuple(health["rounds"])),
        at_action,
        after,
        money,
    )
    assert list(found.said) == kept_verification["said"] and found.verdict == "passed"
    assert saved["removed_by"].startswith(f"Klaxon: its rollback (Klaxon, approved request {request['id']})")
    assert [line["by"] for line in saved["deploys"]] == ["klaxon.faults", f"Klaxon, approved request {request['id']}"]


def test_the_gated_live_runs_record_what_waiting_cost():
    """The damage the money check saw at the alert, at the request, at the action and after it (STATUS, item 2)."""
    saved = json.loads(
        (
            loop.REPO / "evidence" / "week4" / GATED_RUNS["duplicate-delivery, approved"] / fault_runs.RUN_FILE
        ).read_text()
    )
    damage = saved["damage"]
    assert damage["at_alert"]["money_check"]["failed"]["posted_exactly_once"] == 7
    counted = [
        verify.Reading.from_dict(damage[key]).counts()[verify.COUNTED_TWICE]
        for key in ("at_request", "at_action", "after_action")
    ]
    assert counted == [12, 28, 28]  # it grew while the request waited, and not after the rollback
