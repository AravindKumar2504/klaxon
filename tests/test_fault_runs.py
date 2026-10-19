"""The harness for the loop's runs on the faults, offline: the order of things (traffic, a quiet stack, the fault, the
alert, the loop, the fault cleared, the traffic stopped); that the loop gets exactly the alert the rules raised; that
the fault is always cleared and the traffic always stopped; and that nothing the model is given names the fault.

The stack, the traffic and the readings are made up here, and so is every model reply: the command's end-to-end test
runs Klaxon's real loop with scripted replies, and Klaxon's real MCP server code serving made-up tool results."""

import json
import os
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from klaxon import alerts, fault_runs, faults, grading
from klaxon.agent import loop
from klaxon.agent import mcp_client as mc
from klaxon.agent.claude_code import find_claude
from klaxon.agent.contract import Diagnosis, Evidence, ProposedFix
from klaxon.agent.model import ModelError, ScriptedModel
from klaxon.agent.tools import make_tools
from klaxon.agent.trace import read_trace
from klaxon.traffic import Settings
from tests.agent.test_loop import Served, tool
from tests.conftest import NO_CLAUDE
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

    def prometheus(self, query):
        series = HEALTHY | self.before | (self.sick if self.fault_on else {})
        answer = series[next(name for name, text in alerts.QUERIES.items() if text == query)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def money(self):
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
        self.fault_on = False
        return {"made_up": "cleared"}

    def alerting(self):
        return ALERTING(self.prometheus, self.money, clock=lambda: NOW + self.now)

    def stack(self):
        return STACK(None, self.alerting(), lambda s: FakeTraffic(self, s), self.sleep, self.clock)

    def fault(self, name="provider-outage"):
        return replace(faults.FAULTS[name], inject=self.inject, clear=self.clear)


DIAGNOSIS = Diagnosis(
    "The payment provider is timing out, and with no circuit breaker every payment waits through three attempts.",
    (Evidence("c1", "query_metrics", {"query": "x"}, "made up"),),
    0.7,
    ProposedFix("none", "fake-provider", "Wait for the provider; add a circuit breaker."),
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
        "alert_at": 105.0,
        "loop_ended_at": 225.0,
        "cleared_at": 225.0,
    }
    assert record.evaluations[-1]["seconds"] == 225.0 + fault_runs.AFTER_SECONDS
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
    assert record.comparison["verdict"] == "names the cause" and record.comparison["by"] == grading.BY
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
    assert record.evaluations[0]["seconds"] == fault_runs.WARMUP_SECONDS


# The fault is always cleared, and the traffic always stopped


def test_no_alert_in_time_means_no_loop_and_the_fault_is_still_cleared():
    world = World(sick={})  # the fault shows nothing the rules read
    record = run(world)
    assert calls(world) == ["traffic on", "inject", "clear", "traffic closed", "traffic joined"]
    assert record.stopped == "no alert fired within 180 s of the injection"
    assert record.alert is None and record.loop is None and record.comparison is None
    during = [e for e in record.evaluations if e["phase"] == "during"]
    assert len(during) == 12 and during[-1]["seconds"] - record.timings["injected_at"] == 180.0


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
    assert [e["seconds"] for e in found["evaluations"]] == [
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


@pytest.fixture
def offline(monkeypatch):
    """The command with a made-up stack and traffic, and Klaxon's real loop: scripted replies, and Klaxon's real MCP
    server code serving made-up tool results, in this process."""
    world = World()
    monkeypatch.setattr(fault_runs, "preflight", lambda model: Path("/made-up/claude"))
    monkeypatch.setattr(fault_runs.faults, "get", lambda name: world.fault(name))
    monkeypatch.setattr(fault_runs.faults, "LocalStack", lambda: None)
    monkeypatch.setattr(fault_runs.alerts, "Alerting", world.alerting)
    monkeypatch.setattr(fault_runs, "Stack", lambda local, alerting: world.stack())
    monkeypatch.setattr(fault_runs, "_provenance", lambda command: {"rerun_command": command, "git_commit": "made-up"})
    script = [tool("query_metrics", query="sum(rate(klaxon_provider_attempts_total[1m]))"), DIAGNOSE_OUTAGE]
    given = []

    def investigate_live(alert, budgets, folder, header, model_id, binary):
        given.append((alert, header, model_id))
        return loop.investigate(alert, ScriptedModel(script), Served().client, folder / loop.TRACE, budgets)

    monkeypatch.setattr(fault_runs.loop, "investigate_live", investigate_live)
    return world, given


DIAGNOSE_OUTAGE = json.dumps(
    {
        "action": "diagnose",
        "why": "made up for the test",
        "diagnosis": {
            "hypothesis": "The payment provider times out; with no circuit breaker each payment waits through retries.",
            "evidence": [{"call_id": "c1", "shows": "made up"}],
            "confidence": 0.7,
            "proposed_fix": {"kind": "code_change", "target": "payments-api", "summary": "Add a circuit breaker."},
            "blast_radius": "made up",
            "rollback_plan": "made up",
        },
    }
)


def test_the_command_runs_one_fault_and_saves_everything_in_one_folder(offline, tmp_path, capsys):
    world, given = offline
    assert fault_runs.main(["run", "provider-outage", "--evidence-dir", str(tmp_path)]) == 0
    [folder] = tmp_path.iterdir()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_fault-run_provider-outage", folder.name)
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["label"] == fault_runs.LABEL and "not a simulation" in saved["label"]
    assert saved["fault"]["cause"] == faults.FAULTS["provider-outage"].cause
    assert saved["alert"]["name"] == "PaymentsLatencyHigh" and saved["stopped"] == "the loop stopped: diagnosed"
    assert saved["comparison"]["by"] == grading.BY and saved["comparison"]["verdict"] == "names the cause"
    assert saved["rerun_command"].startswith("python -m klaxon.fault_runs run provider-outage")
    [(alert, header, model_id)] = given
    assert model_id == "claude-opus-5-5" and header["alert"]["summary"] == alert.summary
    assert header["fault_name"] == "provider-outage" and header["budgets"]["steps"] == 12
    assert read_trace(folder / loop.TRACE)[-1]["reason"] == "diagnosed"
    out = capsys.readouterr().out
    assert "Compared by a rule (klaxon.grading)" in out and "verdict: NAMES THE CAUSE" in out
    assert fault_runs.main(["grade", str(folder)]) == 0
    assert "verdict: NAMES THE CAUSE" in capsys.readouterr().out


def test_a_run_that_fails_midway_still_saves_its_record_and_says_so(offline, tmp_path, monkeypatch, capsys):
    world, _ = offline
    world.clear_fails = True
    assert fault_runs.main(["run", "provider-outage", "--evidence-dir", str(tmp_path)]) == 2
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["stopped"] == "error: RuntimeError: docker did not answer" and saved["cleared"] is None
    assert "THE FAULT MAY STILL BE IN PLACE: make fault-clear FAULT=provider-outage" in capsys.readouterr().err


def test_an_interrupted_run_saves_its_record(offline, tmp_path, monkeypatch):
    world, _ = offline
    monkeypatch.setattr(fault_runs.loop, "investigate_live", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert fault_runs.main(["run", "provider-outage", "--evidence-dir", str(tmp_path)]) == 130
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["stopped"] == "interrupted" and saved["cleared"] == {"made_up": "cleared"}


def test_a_run_whose_alert_never_fired_exits_1(offline, tmp_path):
    world, given = offline
    world.sick = {}
    assert fault_runs.main(["run", "provider-outage", "--evidence-dir", str(tmp_path)]) == 1
    assert given == []


def test_the_quiet_command_saves_its_evidence(monkeypatch, tmp_path, capsys):
    world = World()
    monkeypatch.setattr(fault_runs.alerts, "Alerting", world.alerting)
    monkeypatch.setattr(fault_runs, "_provenance", lambda command: {"rerun_command": command})
    real = fault_runs.quiet

    def quiet(alerting, settings, minutes):
        return real(alerting, settings, minutes, lambda s: FakeTraffic(world, s), world.sleep, world.clock)

    monkeypatch.setattr(fault_runs, "quiet", quiet)
    assert fault_runs.main(["quiet", "--minutes", "1", "--evidence-dir", str(tmp_path)]) == 0
    [path] = tmp_path.iterdir()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_alerts-under-traffic_rate3_seed7\.json", path.name)
    saved = json.loads(path.read_text())
    assert saved["label"] == fault_runs.QUIET_LABEL and saved["alerts_fired"] == 0
    assert "5 evaluations: 0 with an alert" in capsys.readouterr().out


def test_grading_a_folder_without_a_run_is_refused(tmp_path, capsys):
    assert fault_runs.main(["grade", str(tmp_path)]) == 2
    assert "Cannot grade" in capsys.readouterr().err


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
