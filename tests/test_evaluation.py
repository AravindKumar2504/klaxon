"""The evaluation harness (week 5), offline: the eight cases and their right answers; the withheld tool; the gate that
plans and asks nobody; the false alarm; a suite, seed by seed, each run on a fresh stack, and when it stops; the report
from saved runs, weeks 3 and 4's real ones included; the rehearsals; the command line.

The stacks, the traffic and the readings are made up here, as in tests/test_fault_runs.py, and so is every model reply:
the suite's end-to-end test runs Klaxon's real loop with scripted replies, the real faults deploying into the made-up
docker (tests/fake_docker.py), and Klaxon's real MCP server code serving made-up tool results."""

import json
import re
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from klaxon import alerts, evaluation, fault_runs, faults, grading
from klaxon.agent import catalog, claude_code, gate, loop, verify
from klaxon.agent import mcp_client as mc
from klaxon.agent.contract import Diagnosis, Evidence, FixAction, ProposedFix
from klaxon.agent.mcp_server import MAX_FIELD
from klaxon.agent.model import ModelError, ScriptedModel
from klaxon.agent.tools import Sources, ToolUnavailable, clean_text, make_tools
from klaxon.agent.trace import read_trace
from klaxon.patient.deploys import DeployLog
from klaxon.patient.stack import StackError
from klaxon.traffic import Settings
from tests.agent.test_loop import Served, tool
from tests.fake_docker import GOOD, NAIVE, FakeDocker
from tests.test_fault_runs import (
    LEDGER,
    NAMES_ROLLBACK,
    DockerStack,
    World,
    calls,
    money_checks,
)

REPO = Path(__file__).resolve().parents[1]
CASE_NAMES = [
    "duplicate-delivery",
    "poison-message",
    "provider-outage",
    "money-unit",
    "secret-rotation",
    "pool-exhaustion",
    "false-alarm",
    "missing-deploys",
]
FALSE = evaluation.CASES["false-alarm"]
MISSING = evaluation.CASES["missing-deploys"]
READING = verify.Reading("2026-11-04T00:00:00Z", "passed", 1_792_400_000)
NOTHING_WRONG = Diagnosis(
    "The alert is false: the ledger consumer group's lag reads 0 under traffic, so nothing is behind.",
    (Evidence("c1", "query_metrics", {"query": "x"}, "made up"),),
    0.9,
    ProposedFix("none", "ledger-consumer", "Nothing to do."),
    "none",
    "none",
)
NOTHING_WRONG_REPLY = json.dumps(
    {
        "action": "diagnose",
        "why": "made up for the test",
        "diagnosis": {
            "hypothesis": "The alert is false: the ledger consumer group's lag reads 0 under traffic.",
            "evidence": [{"call_id": "c1", "shows": "made up"}],
            "confidence": 0.9,
            "proposed_fix": {"kind": "none", "target": "ledger-consumer", "summary": "Nothing to do."},
            "blast_radius": "none",
            "rollback_plan": "none",
        },
    }
)


def case_record(case, fault=None, seed=7):
    described = case.describe() | {"seed": seed, "model": "claude-opus-5-5"}
    return fault_runs.Record(
        described if fault is None else fault.describe(), {"rate": 3.0}, {"steps": 10}, case=described
    )


def diagnosed(world, diagnosis, seconds=40.0):
    def investigate(alert):
        world.calls.append(("investigate", alert))
        world.now += seconds
        return loop.RunResult("diagnosed", "confidence 0.9", diagnosis, 4, 3, seconds, Path("trace.jsonl"))

    return investigate


# The cases


def test_eight_cases_each_with_its_kind_and_right_answer():
    assert list(evaluation.CASES) == CASE_NAMES
    kinds = {name: case.kind for name, case in evaluation.CASES.items()}
    assert kinds == {name: evaluation.FAULT for name in CASE_NAMES[:6]} | {
        "false-alarm": evaluation.FALSE_ALARM,
        "missing-deploys": evaluation.MISSING,
    }
    for name in CASE_NAMES[:6]:
        case = evaluation.CASES[name]
        assert case.fault == name and faults.FAULTS[name].cause in case.right_answer and not case.withheld
    assert FALSE.fault is None and "Nothing is wrong" in FALSE.right_answer and FALSE.right_action is None
    assert MISSING.fault == "duplicate-delivery" and MISSING.withheld == ("recent_deploys",)
    assert "could not be checked" in MISSING.right_answer and "claims no deploy it did not see" in MISSING.right_answer
    with pytest.raises(ValueError, match="no case 'nope'"):
        evaluation.get_case("nope")


def test_the_action_that_fits_each_case_agrees_with_the_rubric_committed_before_the_runs():
    # Rubric version 3 (75fedf1): a rollback fits faults 1, 2, 4, 5 and 6, on its own service; nothing fits fault 3.
    for name, case in evaluation.CASES.items():
        if case.fault is None:
            assert case.right_action is None
            continue
        rubric = grading.RUBRICS_V3[case.fault]
        if "rollback" not in rubric.fix_kinds:
            assert case.right_action is None, name
            continue
        action, service = case.right_action
        assert action == catalog.ROLLBACK_DEPLOY.name and service in catalog.ROLLBACK_SERVICES, name
        assert re.search(rubric.fix_target, service, re.IGNORECASE), name
    assert evaluation.CASES["provider-outage"].right_action is None
    assert MISSING.right_action == evaluation.CASES["duplicate-delivery"].right_action


def test_the_false_alarms_alert_is_the_lag_rules_own_words_with_a_made_up_number():
    alert = evaluation.false_alert("2026-11-04T00:00:00Z")
    assert alert == loop.Alert(
        "ConsumerLagHigh",
        "The ledger consumer group is 250 messages behind on the topic provider-events, all partitions together;"
        " the threshold is 50.",
        "2026-11-04T00:00:00Z",
    )
    assert evaluation.FALSE_LAG > alerts.LAG_MESSAGES  # the rule would fire at this number


def test_the_withheld_tool_says_what_the_real_tool_says_when_docker_does_not_answer():
    def silent(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 15)

    tools = make_tools(Sources(ensure_local=silent, containers=silent, inspect=silent))
    with pytest.raises(ToolUnavailable) as raised:
        tools["recent_deploys"].call({})
    assert clean_text(str(raised.value), 2 * MAX_FIELD) == evaluation.DOCKER_SILENT  # as the server sends it
    assert evaluation.OUTAGES == {"recent_deploys": (evaluation.DOCKER_SILENT, 15.0)}


def test_the_cases_command_prints_each_case_its_right_answer_and_its_action(capsys):
    assert evaluation.main(["cases"]) == 0
    out = capsys.readouterr().out
    for case in evaluation.CASES.values():
        assert f"{case.name} ({case.kind}): {case.title}" in out and case.right_answer in out
    assert "action that fits: rollback_deploy payments-api" in out and "action that fits: none" in out
    assert f"withheld: recent_deploys: each call gets {evaluation.DOCKER_SILENT!r}" in out


# The withheld tool


class Inner:
    """A tool client that only says what it was asked: no server."""

    def __init__(self):
        self.called, self.opened, self.closed = [], False, False
        self.server_info = {"name": "made up"}

    def open(self, timeout=None):
        self.opened = True
        return [mc.ToolSpec(t.name, t.title, t.description, t.input_schema, {}) for t in make_tools().values()]

    def call(self, name, arguments, timeout):
        self.called.append((name, dict(arguments)))
        return mc.ToolResult(f"{name} from the server", False)

    def close(self):
        self.closed = True


def test_a_withheld_tool_never_reaches_its_source_and_answers_as_late_as_a_real_one():
    inner, slept = Inner(), []
    client = evaluation.Withheld(inner, ["recent_deploys"], sleep=slept.append)
    assert [spec.name for spec in client.open()] == list(make_tools()) and inner.opened
    assert client.call("recent_deploys", {}, 60.0) == mc.ToolResult(evaluation.DOCKER_SILENT, True)
    assert inner.called == [] and slept == [15.0]
    assert client.call("service_health", {}, 60.0) == mc.ToolResult("service_health from the server", False)
    assert inner.called == [("service_health", {})] and client.server_info == {"name": "made up"}
    assert client.call("recent_deploys", {}, 5.0).is_error and slept[-1] == 5.0  # never past the call's time left
    client.close()
    assert inner.closed


def test_a_withheld_call_with_arguments_the_tool_refuses_gets_the_servers_own_answer():
    inner = Inner()
    client = evaluation.Withheld(inner, ["recent_deploys"], sleep=lambda seconds: None)
    client.open()
    assert client.call("recent_deploys", {"since": "today"}, 60.0).text == "recent_deploys from the server"
    assert inner.called == [("recent_deploys", {"since": "today"})]


def test_a_withheld_tools_answer_is_recorded_and_replays(tmp_path):
    path = tmp_path / "tool-results.jsonl"
    recorded = mc.RecordedTools(evaluation.Withheld(Inner(), ["recent_deploys"], sleep=lambda s: None), path)
    recorded.open()
    assert recorded.call("recent_deploys", {}, 60.0).is_error
    replayed = mc.ReplayTools(path)
    assert replayed.call("recent_deploys", {}, 60.0) == mc.ToolResult(evaluation.DOCKER_SILENT, True)


def test_a_live_run_records_the_results_of_the_client_it_is_given(tmp_path, monkeypatch):
    # The missing-deploys case hands the loop its own client: the run records what that client answered.
    script = [tool("recent_deploys"), NOTHING_WRONG_REPLY]
    monkeypatch.setattr(claude_code, "ClaudeCode", lambda recording, model, binary: ScriptedModel(script))
    client = evaluation.Withheld(Served().client, ["recent_deploys"], sleep=lambda s: None)
    alert = evaluation.false_alert("2026-11-04T00:00:00Z")
    found = loop.investigate_live(alert, loop.Budgets(), tmp_path, {}, "claude-opus-5-5", Path("x"), tool_client=client)
    assert found.reason == "diagnosed"
    [called] = [r for r in map(json.loads, (tmp_path / loop.TOOL_RESULTS).read_text().splitlines()) if r.get("seq")]
    assert (called["tool"], called["is_error"], called["text"]) == ("recent_deploys", True, evaluation.DOCKER_SILENT)


# The gate of an evaluation run


def test_the_dry_gate_reads_the_money_at_every_diagnosis_then_plans_and_asks_nobody(tmp_path):
    docker = FakeDocker()
    deployer = docker.deployer(DeployLog(tmp_path / "deploys.jsonl"))
    deployer.deploy(LEDGER, NAIVE, by="klaxon.faults")
    damage, reads = {}, []

    def read():
        reads.append(1)
        return READING

    dry = evaluation.dry_gate(tmp_path, deployer, damage, read)
    alert = evaluation.false_alert("2026-11-04T00:00:00Z")
    outcome = dry.act(alert, NOTHING_WRONG, lambda: pytest.fail("nobody is asked"))
    assert outcome.result == "no action" and damage["at_diagnosis"] == READING.to_dict() and reads == [1]
    named = replace(NOTHING_WRONG, fix_action=FixAction("rollback_deploy", {"service": LEDGER}))
    outcome = dry.act(alert, named, lambda: pytest.fail("nobody is asked"))
    assert outcome.result == "not asked" and outcome.detail.startswith("redeploy ledger-consumer: LEDGER_HANDLER")
    assert docker.settings(LEDGER) == NAIVE and not (tmp_path / gate.REQUEST).exists() and reads == [1, 1]
    assert dry.decided() is None
    for part in (dry.verify_action, dry.propose_undo):
        with pytest.raises(RuntimeError, match="runs no action"):
            part(None, None) if part is dry.propose_undo else part(None)


# The false alarm


def false_alarm(world, investigate, gated=None, case=FALSE):
    record = case_record(case)
    return evaluation.false_alarm(case, record, world.stack(), Settings(seed=7, run_id="r1"), investigate, gated)


def test_a_false_alarm_goes_from_a_quiet_stack_to_the_given_alert_to_the_loop_and_injects_nothing():
    world = World()
    record = false_alarm(world, diagnosed(world, NOTHING_WRONG))
    assert calls(world) == ["traffic on", "investigate", "traffic closed", "traffic joined"]
    assert [e["phase"] for e in record.evaluations] == ["before", "before", "after"]
    assert record.alert == asdict(evaluation.false_alert(record.evaluations[1]["at_utc"]))
    assert world.calls[1][1] == evaluation.false_alert(record.evaluations[1]["at_utc"])  # exactly that alert
    assert record.injected is None and record.cleared is None and "injected_at" not in record.timings
    assert record.timings["alert_given_at"] <= record.timings["loop_started_at"] < record.timings["loop_ended_at"]
    assert record.damage["at_alert"]["money_check"]["outcome"] == "passed"
    assert record.stopped == "the loop stopped: diagnosed" and record.loop["steps"] == 4
    assert record.evaluations[-1]["began"] >= record.timings["loop_ended_at"] and record.traffic is not None


def test_no_false_alarm_is_given_on_a_stack_that_is_not_quiet_and_the_traffic_still_stops():
    world = World(before={"p95_seconds": [({}, 9.37)]})
    record = false_alarm(world, diagnosed(world, NOTHING_WRONG))
    assert record.stopped.startswith("not quiet before the false alarm, so none was given")
    assert record.alert is None and record.loop is None
    assert calls(world) == ["traffic on", "traffic closed", "traffic joined"]


def test_a_false_alarm_needs_a_quiet_stack_at_two_evaluations_in_a_row():
    world = World()
    alerting = world.alerting()
    real = alerting.evaluate
    seen = []

    def evaluate():
        seen.append(1)
        if len(seen) == 2:
            world.before = {"p95_seconds": [({}, 9.37)]}
        return real()

    alerting.evaluate = evaluate
    stack = fault_runs.Stack(None, alerting, world.stack().new_traffic, world.sleep, world.clock)
    record = evaluation.false_alarm(
        FALSE, case_record(FALSE), stack, Settings(run_id="r1"), diagnosed(world, NOTHING_WRONG), None
    )
    assert record.alert is None and record.loop is None and len(record.evaluations) == 2


def test_a_false_alarms_diagnosis_goes_through_the_dry_gate_and_its_money_is_read(tmp_path):
    world = World()
    record = case_record(FALSE)
    dry = evaluation.dry_gate(
        tmp_path, FakeDocker().deployer(DeployLog(tmp_path / "d.jsonl")), record.damage, lambda: READING
    )
    evaluation.false_alarm(FALSE, record, world.stack(), Settings(run_id="r1"), diagnosed(world, NOTHING_WRONG), dry)
    assert record.gate == {
        "result": "no action",
        "detail": "the diagnosis names no action",
        "request_id": None,
        "deploy": None,
        "decided": None,
    }
    assert record.damage["at_diagnosis"]["outcome"] == "passed" and record.gated


def test_the_traffic_stops_even_when_a_false_alarms_loop_fails():
    world = World()

    def broken(alert):
        raise RuntimeError("the loop broke")

    with pytest.raises(RuntimeError, match="the loop broke"):
        false_alarm(world, broken)
    assert calls(world)[-2:] == ["traffic closed", "traffic joined"]


def test_a_false_alarms_rehearsal_reads_the_rules_a_minute_more_and_calls_no_model():
    world = World()
    record = false_alarm(world, None)
    assert record.rehearsal and record.loop is None and record.alert is not None
    assert [e["phase"] for e in record.evaluations] == ["before", "before", "during", "during", "during", "during"]
    assert record.stopped.startswith("a rehearsal") and "investigate" not in calls(world)


@pytest.mark.parametrize(
    "where, said, never",
    [
        ("traffic", "stopped before its synthetic traffic began: no alert was given", "loop"),
        ("quiet", "No alert was given. The run stopped: not quiet", "Klaxon's loop"),
        ("rehearsal", "A rehearsal: no model was called", "Klaxon's loop"),
        ("done", "Klaxon's loop ran live on it", "approved"),
    ],
)
def test_a_false_alarms_label_claims_nothing_it_did_not_do(where, said, never, tmp_path):
    world = World(before={"p95_seconds": [({}, 9.37)]} if where == "quiet" else None)
    record = case_record(FALSE)
    if where == "traffic":
        world.traffic_fails = True
        with pytest.raises(httpx.ConnectError):
            evaluation.false_alarm(FALSE, record, world.stack(), Settings(run_id="r1"), None, None)
    else:
        investigate = None if where == "rehearsal" else diagnosed(world, NOTHING_WRONG)
        dry = evaluation.dry_gate(
            tmp_path, FakeDocker().deployer(DeployLog(tmp_path / "d.jsonl")), record.damage, lambda: READING
        )
        evaluation.false_alarm(FALSE, record, world.stack(), Settings(run_id="r1"), investigate, dry)
    text = evaluation.label(record)
    assert said in text and never not in text
    if where == "done":
        assert fault_runs.EVALUATION_OFFERED in text and "Its diagnosis named no action" in text
        assert "No fault was injected: an evaluation case, a false alarm" in text
        assert "written by the harness in the lag rule's own words" in text


def test_a_false_alarms_evidence_is_saved_and_printed(tmp_path, capsys):
    world = World()
    record = case_record(FALSE)
    false_alarm_run = evaluation.false_alarm
    false_alarm_run(FALSE, record, world.stack(), Settings(run_id="r1"), diagnosed(world, NOTHING_WRONG), None)
    evaluation.save(tmp_path, record, {"case": "false-alarm"})
    saved = json.loads((tmp_path / fault_runs.RUN_FILE).read_text())
    assert saved["label"] == evaluation.label(record) and saved["case"]["name"] == "false-alarm"
    printed = saved["printed_output"]
    assert printed[0] == "Klaxon week 5: an evaluation run (case false-alarm, seed 7): a false alarm"
    assert any("written by the harness, not raised by the rules" in line for line in printed)
    assert "Saved in" in capsys.readouterr().out


# An evaluation run on a fault: the real fault deploys into the made-up docker, and the dry gate plans on it


def fault_case_run(tmp_path, case, diagnosis_reply=None, named=True):
    world = DockerStack()
    fault = faults.get(case.fault)
    record = case_record(case, fault)
    dry = evaluation.dry_gate(tmp_path, world.local.deployer, record.damage, lambda: READING)
    found = Diagnosis(
        "The ledger consumer was redeployed with a handler that books resent webhooks twice.",
        (Evidence("c1", "money_check", {"wait": True}, "made up"),),
        0.8,
        ProposedFix("rollback", LEDGER, "Roll it back."),
        "made up",
        "made up",
        FixAction("rollback_deploy", {"service": LEDGER}) if named else None,
    )
    fault_runs.run(fault, record, world.stack(), Settings(seed=7, run_id="r1"), diagnosed(world, found), gated=dry)
    return world, record


def test_an_evaluation_run_on_a_fault_plans_the_rollback_asks_nobody_and_the_harness_clears_it(tmp_path):
    world, record = fault_case_run(tmp_path, evaluation.CASES["duplicate-delivery"])
    assert record.gate["result"] == "not asked" and record.gate["request_id"] is None
    assert record.verification is None and record.undo is None
    assert list(record.damage) == ["at_alert", "at_diagnosis"]  # no request, so no damage "at the request"
    assert record.removed_by == "the harness's clearing (klaxon.faults)"
    assert [line["by"] for line in record.deploys] == ["klaxon.faults", "klaxon.faults"]
    assert world.docker.settings(LEDGER) == GOOD[LEDGER] and not (tmp_path / gate.REQUEST).exists()
    text = fault_runs.label(record)
    assert fault_runs.EVALUATION_OFFERED in text and "which runs only if a person approves it" not in text
    assert "Code planned the action its diagnosis named (redeploy ledger-consumer:" in text
    assert "in an evaluation run nobody is asked, and nothing ran." in text and "approved" not in text
    assert "withheld" not in text
    printed = fault_runs.render(record).splitlines()
    assert (
        printed[0] == "Klaxon week 5: an evaluation run on case duplicate-delivery, seed 7, a fault: duplicate-delivery"
    )
    assert "The gate: not asked: redeploy ledger-consumer: LEDGER_HANDLER naive -> idempotent" in "\n".join(printed)


def test_an_evaluation_runs_label_says_which_tool_the_harness_withheld(tmp_path):
    _, record = fault_case_run(tmp_path, MISSING)
    assert fault_runs.withheld_sentence({"recent_deploys": evaluation.DOCKER_SILENT}) in fault_runs.label(record)
    assert "The harness withheld recent_deploys: each call to it got the error" in fault_runs.label(record)


# A suite


def outcome(case, seed, loop_reason="diagnosed", stopped=None, interrupted=False):
    return {
        "case": case.name,
        "seed": seed,
        "folder": f"{case.name}_seed{seed}",
        "stopped": stopped or f"the loop stopped: {loop_reason}",
        "loop": loop_reason,
        "interrupted": interrupted,
    }


def suite_of(planned, results=None, fresh_fails_at=None, fresh_raises=None):
    done, written = [], []
    results = results or {}
    fresh_raises = fresh_raises or StackError("docker did not answer")

    def fresh():
        done.append("fresh")
        if fresh_fails_at is not None and done.count("fresh") == fresh_fails_at:
            raise fresh_raises

    def run_one(case, seed):
        done.append((case.name, seed))
        found = results.get((case.name, seed)) or outcome(case, seed)
        if isinstance(found, BaseException):
            raise found
        return found

    suite = evaluation.run_suite(
        planned, {}, lambda value: written.append(json.loads(json.dumps(value))), fresh, run_one
    )
    return suite, done, written


def test_a_suite_runs_seed_by_seed_each_run_on_a_fresh_stack():
    cases = [evaluation.CASES["duplicate-delivery"], FALSE]
    planned = evaluation.plan(cases, [7, 8])
    assert [(case.name, seed) for case, seed in planned] == [
        ("duplicate-delivery", 7),
        ("false-alarm", 7),
        ("duplicate-delivery", 8),
        ("false-alarm", 8),
    ]
    suite, done, written = suite_of(planned)
    assert done == [
        "fresh", ("duplicate-delivery", 7), "fresh", ("false-alarm", 7),
        "fresh", ("duplicate-delivery", 8), "fresh", ("false-alarm", 8),
    ]  # fmt: skip
    assert len(suite["runs"]) == 4 and suite["stopped"] is None and suite["not_run"] == []
    assert len(written[1]["runs"]) == 1  # the suite's file is written after each run


@pytest.mark.parametrize("reason", ["model_error", "tools_failed", "crashed"])
def test_a_suite_stops_after_a_run_that_says_nothing_of_klaxon_and_would_likely_fail_again(reason):
    planned = evaluation.plan([evaluation.CASES["duplicate-delivery"], FALSE], [7, 8])
    results = {("false-alarm", 7): outcome(FALSE, 7, reason)}
    suite, done, _ = suite_of(planned, results)
    assert [step for step in done if step != "fresh"] == [("duplicate-delivery", 7), ("false-alarm", 7)]  # never again
    assert suite["stopped"] == f"after false-alarm_seed7, because its loop stopped on {reason}"
    assert suite["not_run"] == [{"case": "duplicate-delivery", "seed": 8}, {"case": "false-alarm", "seed": 8}]


@pytest.mark.parametrize(
    "result, why",
    [
        (dict(interrupted=True), "it was interrupted"),
        (dict(stopped="error: StackError: docker did not answer", loop_reason=None), "it stopped on an error"),
    ],
)
def test_a_suite_stops_at_an_interrupt_or_an_error_outside_the_loop(result, why):
    planned = evaluation.plan([FALSE], [7, 8])
    suite, done, _ = suite_of(planned, {("false-alarm", 7): outcome(FALSE, 7, **result)})
    assert why in suite["stopped"] and suite["not_run"] == [{"case": "false-alarm", "seed": 8}]


def test_a_suite_goes_on_after_a_run_klaxon_got_wrong_or_whose_fault_raised_no_alert():
    planned = evaluation.plan([evaluation.CASES["duplicate-delivery"]], [7, 8, 9])
    results = {
        ("duplicate-delivery", 7): outcome(planned[0][0], 7, "step_budget"),
        ("duplicate-delivery", 8): outcome(planned[0][0], 8, None, "no alert fired within 180 s of the injection"),
    }
    suite, done, _ = suite_of(planned, results)
    assert len(suite["runs"]) == 3 and suite["stopped"] is None


@pytest.mark.parametrize(
    "raised, stopped",
    [
        (StackError("docker is not this Mac's"), "false-alarm, seed 8, could not start: StackError: docker is not"),
        (KeyboardInterrupt(), "interrupted as false-alarm, seed 8, was starting"),
    ],
)
def test_a_suite_stops_when_a_run_cannot_even_start(raised, stopped):
    planned = evaluation.plan([FALSE], [7, 8, 9])
    suite, done, written = suite_of(planned, {("false-alarm", 8): raised})
    assert suite["stopped"].startswith(stopped) and len(suite["runs"]) == 1
    assert suite["not_run"] == [{"case": "false-alarm", "seed": 8}, {"case": "false-alarm", "seed": 9}]
    assert written[-1] == suite


def test_a_suite_interrupted_while_its_stack_is_made_fresh_says_so():
    planned = evaluation.plan([FALSE], [7, 8])
    suite, done, _ = suite_of(planned, fresh_fails_at=2, fresh_raises=KeyboardInterrupt())
    assert suite["stopped"] == "interrupted while the stack was made fresh"
    assert suite["not_run"] == [{"case": "false-alarm", "seed": 8}] and len(suite["runs"]) == 1


def test_a_suite_stops_when_the_stack_cannot_be_made_fresh():
    planned = evaluation.plan([FALSE], [7, 8, 9])
    suite, done, _ = suite_of(planned, fresh_fails_at=2)
    assert suite["stopped"] == "the stack could not be reset and started: docker did not answer"
    assert suite["not_run"] == [{"case": "false-alarm", "seed": 8}, {"case": "false-alarm", "seed": 9}]
    assert len(suite["runs"]) == 1


# The suite's command, end to end, offline


def patch_suite(monkeypatch, world):
    monkeypatch.setattr(evaluation.fault_runs, "preflight", lambda model: Path("/made-up/claude"))
    monkeypatch.setattr(evaluation, "fresh_stack", lambda: world.calls.append("fresh stack"))
    monkeypatch.setattr(evaluation.faults, "LocalStack", lambda: world.local)
    monkeypatch.setattr(evaluation.alerts, "Alerting", world.alerting)
    monkeypatch.setattr(evaluation.fault_runs, "Stack", lambda local, alerting: world.stack())
    monkeypatch.setattr(evaluation.fault_runs, "_provenance", lambda command: {"rerun_command": command})
    monkeypatch.setattr(evaluation.live, "check_live", money_checks(world))
    scripts = {
        "MoneyCheckFailed": [tool("recent_deploys"), NAMES_ROLLBACK],
        "ConsumerLagHigh": [tool("query_metrics", query="sum(klaxon_consumer_lag)"), NOTHING_WRONG_REPLY],
    }

    def investigate_live(alert, budgets, folder, header, model_id, binary, actions=(), tool_client=None):
        client = Served().client
        if tool_client is not None:  # the case's own client, around the made-up server instead of the real one
            client = evaluation.Withheld(client, tool_client.outages, sleep=world.sleep)
        model = ScriptedModel(scripts[alert.name])
        return loop.investigate(alert, model, client, folder / loop.TRACE, budgets, actions=actions)

    monkeypatch.setattr(evaluation.loop, "investigate_live", investigate_live)


def test_the_suite_command_runs_each_case_on_a_fresh_stack_and_writes_the_report(monkeypatch, tmp_path, capsys):
    world = DockerStack()
    patch_suite(monkeypatch, world)
    argv = ["run", "--cases", "duplicate-delivery,false-alarm,missing-deploys", "--seeds", "7"]
    assert evaluation.main([*argv, "--evidence-dir", str(tmp_path)]) == 0
    [suite_folder] = tmp_path.iterdir()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_suite_claude-opus-5-5", suite_folder.name)
    suite = json.loads((suite_folder / evaluation.SUITE_FILE).read_text())
    assert [run["case"] for run in suite["runs"]] == ["duplicate-delivery", "false-alarm", "missing-deploys"]
    assert suite["stopped"] is None and suite["budgets"]["steps"] == 10 and suite["budgets"]["tool_calls"] == 8
    assert [call for call in world.calls if call == "fresh stack"] == ["fresh stack"] * 3
    runs = {run["case"]: suite_folder / run["folder"] for run in suite["runs"]}
    saved = {name: json.loads((folder / fault_runs.RUN_FILE).read_text()) for name, folder in runs.items()}
    assert saved["duplicate-delivery"]["gate"]["result"] == "not asked"
    assert saved["missing-deploys"]["gate"]["result"] == "not asked"
    assert saved["false-alarm"]["gate"]["result"] == "no action" and saved["false-alarm"]["injected"] is None
    assert all(s["case"]["model"] == "claude-opus-5-5" and s["case"]["seed"] == 7 for s in saved.values())
    assert "The harness withheld recent_deploys" in saved["missing-deploys"]["label"]
    # The withheld tool's answer, as the loop saw it; and fault 1's own run got the made-up server's.
    observed = {
        name: read_trace(folder / loop.TRACE)[1]["came_back"]["text"]
        for name, folder in runs.items()
        if name != "false-alarm"
    }
    assert evaluation.DOCKER_SILENT in observed["missing-deploys"]
    assert evaluation.DOCKER_SILENT not in observed["duplicate-delivery"]
    assert world.docker.settings(LEDGER) == GOOD[LEDGER]  # every fault cleared, and nothing of Klaxon's ran
    found = json.loads((suite_folder / evaluation.REPORT_JSON).read_text())
    assert [row["case"] for row in found["runs"]] == ["duplicate-delivery", "false-alarm", "missing-deploys"]
    assert found["from"] == [evaluation.clean_text(str(suite_folder))]  # never this Mac's home folder by name
    assert (suite_folder / evaluation.REPORT_MD).read_text().startswith("# Klaxon evaluation report")
    out = capsys.readouterr().out
    assert out.startswith(f"The suite's folder: {suite_folder}; 3 runs planned")


def test_the_suite_command_refuses_bad_cases_or_seeds_before_touching_anything(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(evaluation.fault_runs, "preflight", lambda model: pytest.fail("pre-flight ran"))
    monkeypatch.setattr(evaluation, "fresh_stack", lambda: pytest.fail("the stack was touched"))
    for bad in (["--cases", "nope"], ["--seeds", "7,7"], ["--seeds", ""], ["--steps", "1"], ["--rate", "0"]):
        assert evaluation.main(["run", *bad, "--evidence-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == [] and "Not started, and nothing was touched" in capsys.readouterr().err


def test_a_suite_whose_model_could_not_answer_stops_and_exits_1(monkeypatch, tmp_path):
    world = DockerStack()
    patch_suite(monkeypatch, world)

    def investigate_live(alert, budgets, folder, header, model_id, binary, actions=(), tool_client=None):
        refusing = ScriptedModel([ModelError("the seat's usage limit was reached (made up)")] * 2)  # and its retry
        return loop.investigate(alert, refusing, Served().client, folder / loop.TRACE, budgets, actions=actions)

    monkeypatch.setattr(evaluation.loop, "investigate_live", investigate_live)
    argv = ["run", "--cases", "false-alarm", "--seeds", "7,8", "--evidence-dir", str(tmp_path)]
    assert evaluation.main(argv) == 1
    [suite_folder] = tmp_path.iterdir()
    suite = json.loads((suite_folder / evaluation.SUITE_FILE).read_text())
    assert len(suite["runs"]) == 1 and suite["not_run"] == [{"case": "false-alarm", "seed": 8}]
    found = json.loads((suite_folder / evaluation.REPORT_JSON).read_text())
    assert found["runs"][0]["not_run"].startswith("the loop stopped on model_error")


# The report


def test_the_report_reads_weeks_3_and_4s_saved_runs_and_their_measured_numbers():
    # The numbers STATUS gives for them: 12 fault runs, 82 loop calls, $4.38 (Claude Code's estimates, rounded).
    found = evaluation.report([REPO / "evidence" / "week3", REPO / "evidence" / "week4"])
    rows = found["runs"]
    assert len(rows) == 12 and len(found["left_out"]) == 4  # the four rehearsals, with no model
    assert sum(row["calls"] for row in rows) == 82 and round(sum(row["cost_usd"] for row in rows), 2) == 4.38
    assert {row["mode"] for row in rows} == {"diagnosis only, week 3", "gated, week 4"}
    assert all(row["verdict"] == "names the cause" and row["not_run"] is None for row in rows)
    assert all(row["answered_by"] == ["claude-opus-5-5"] for row in rows)
    gates = sorted(row["gate"] for row in rows if row["mode"] == "gated, week 4")
    assert gates == ["done"] * 5 + ["expired"] * 3 + ["no action"]
    week3 = [row for row in rows if row["mode"] == "diagnosis only, week 3"]
    assert all(row["action_right"] is None and row["named_action"] is None for row in week3)  # none was offered
    money_unit = [row for row in rows if row["folder"] == "2026-10-24T082544Z_gated-run_money-unit"][0]
    assert money_unit["damage_at_alert"] == {"outcome": "failed", "findings": {"payments_match_provider": 56}}
    assert money_unit["damage_at_diagnosis"]["findings"] == {"payments_match_provider": 149}  # at its request
    assert money_unit["alert_after_fault_seconds"] == 17.4 and money_unit["action_right"] is True
    summary = {entry["case"]: entry for entry in found["summary"]}
    assert summary["provider-outage"]["fix_fits"] == 1 and summary["provider-outage"]["graded"] == 2
    assert summary["poison-message"]["runs"] == 3 and summary["poison-message"]["calls"] == 21


def saved_run(folder, fault_name="duplicate-delivery", **changes):
    folder.mkdir(parents=True)
    run = {
        "fault": {"name": fault_name},
        "case": None,
        "gated": True,
        "rehearsal": False,
        "traffic_settings": {"seed": 7},
        "timings": {"injected_at": 76.0, "alert_evaluation_began": 94.0},
        "loop": {
            "reason": "diagnosed",
            "detail": "confidence 0.8",
            "steps": 5,
            "tool_calls": 4,
            "seconds": 40.04,
            "diagnosis": {
                "confidence": 0.8,
                "fix_action": {"name": "rollback_deploy", "arguments": {"service": LEDGER}},
            },
        },
        "comparison": {"verdict": "names the cause", "fix_fits_the_remedy": True, "rubric": {"version": 3}},
        "gate": {"result": "not asked"},
        "damage": {},
    }
    for key, value in changes.items():
        run[key] = value
    (folder / fault_runs.RUN_FILE).write_text(json.dumps(run))
    return folder


def responses(folder, *costs, accepted=True, model="claude-opus-5-5"):
    lines = [{"kind": "header"}]
    lines += [{"kind": "response", "model": model, "accepted": accepted, "cost_usd": cost} for cost in costs]
    (folder / loop.RECORDING).write_text("".join(json.dumps(line) + "\n" for line in lines))


def test_a_run_that_says_nothing_of_klaxon_counts_apart_never_as_wrong(tmp_path):
    saved_run(tmp_path / "a")
    saved_run(
        tmp_path / "b",
        loop={
            "reason": "model_error",
            "detail": "a limit",
            "steps": 1,
            "tool_calls": 0,
            "seconds": 1.0,
            "diagnosis": None,
        },
        comparison={"verdict": "no diagnosis"},
    )
    saved_run(tmp_path / "c", loop=None, comparison=None, stopped="no alert fired within 180 s of the injection")
    saved_run(
        tmp_path / "d",
        loop={"reason": "step_budget", "detail": "x", "steps": 10, "tool_calls": 8, "seconds": 99.0, "diagnosis": None},
        comparison={"verdict": "no diagnosis"},
    )
    found = evaluation.report([tmp_path])
    rows = {row["folder"]: row for row in found["runs"]}
    assert rows["a"]["not_run"] is None and rows["d"]["not_run"] is None  # out of steps: Klaxon's own failure
    assert rows["b"]["not_run"] == "the loop stopped on model_error: a limit"
    assert rows["c"]["not_run"] == "the loop never ran: no alert fired within 180 s of the injection"
    [entry] = found["summary"]
    assert (entry["runs"], entry["not_run"], entry["ran"], entry["diagnosed"]) == (4, 2, 2, 1)
    assert (entry["graded"], entry["names_the_cause"]) == (1, 1)
    page = evaluation.markdown(found)
    assert "| duplicate-delivery | 2 ran, 2 not run | 1 of 1 |" in page and "## Not run" in page


def test_the_report_counts_an_action_named_where_none_fits_and_a_plan_code_refused(tmp_path):
    named = {
        "reason": "diagnosed",
        "detail": "x",
        "steps": 4,
        "tool_calls": 3,
        "seconds": 30.0,
        "diagnosis": {"confidence": 0.4, "fix_action": {"name": "rollback_deploy", "arguments": {"service": LEDGER}}},
    }
    case = FALSE.describe() | {"seed": 8, "model": "claude-sonnet-5-5"}
    saved_run(tmp_path / "a", "false-alarm", case=case, loop=named, comparison=None, gate={"result": "refused"})
    [row] = evaluation.report([tmp_path])["runs"]
    assert row["action_where_none_fits"] is True and row["action_right"] is False and row["gate"] == "refused"
    assert row["verdict"] is None and row["model"] == "claude-sonnet-5-5" and row["mode"] == "evaluation"
    [entry] = evaluation.summarize([row])
    assert (entry["action_where_none_fits"], entry["refused_by_code"], entry["action_right"]) == (1, 1, 0)
    assert "not graded yet" in evaluation.markdown(evaluation.report([tmp_path]))


def test_the_report_sums_every_call_and_its_cost_and_gives_medians(tmp_path):
    for name, costs, seconds in (("a", (0.05, 0.06), 30.0), ("b", (0.1,), 50.0), ("c", (0.02, None, 0.03), 41.0)):
        loop_ = {
            "reason": "diagnosed",
            "detail": "x",
            "steps": len(costs) + 1,
            "tool_calls": 1,
            "seconds": seconds,
            "diagnosis": {"confidence": 0.8, "fix_action": None},
        }
        responses(saved_run(tmp_path / name, loop=loop_), *costs, accepted=name != "b")
    found = evaluation.report([tmp_path])
    rows = {row["folder"]: row for row in found["runs"]}
    assert (rows["a"]["calls"], rows["a"]["cost_usd"]) == (2, 0.11)
    assert (rows["b"]["calls"], rows["b"]["accepted_calls"]) == (1, 0)  # every call counts, accepted or not
    assert (rows["c"]["cost_usd"], rows["c"]["calls_without_cost"]) == (0.05, 1)
    [entry] = found["summary"]
    assert entry["median_diagnosis_seconds"] == 41.0 and entry["median_calls"] == 2 and entry["cost_usd"] == 0.26
    assert entry["median_alert_after_fault_seconds"] == 18.0 and entry["calls"] == 6
    assert "In all: 3 runs (0 not run), 6 model calls, $0.26." in evaluation.markdown(found)


def test_the_report_names_its_folders_from_the_repo_never_by_this_macs_home_folder():
    found = evaluation.report([REPO / "evidence" / "week4"])
    assert found["from"] == ["evidence/week4"]
    assert str(Path.home()) not in json.dumps(found) and str(Path.home()) not in evaluation.markdown(found)


def test_the_report_command_writes_its_files_and_refuses_a_run_it_cannot_read(tmp_path, capsys):
    saved_run(tmp_path / "runs" / "a")
    out = tmp_path / "out"
    out.mkdir()
    assert evaluation.main(["report", str(tmp_path / "runs"), "--write", str(out)]) == 0
    assert json.loads((out / evaluation.REPORT_JSON).read_text())["runs"][0]["folder"] == "a"
    assert (out / evaluation.REPORT_MD).read_text() == capsys.readouterr().out.removesuffix("\n")
    assert evaluation.main(["report", str(tmp_path / "runs"), "--write", str(tmp_path / "no-such-folder")]) == 2
    assert "Cannot make the report" in capsys.readouterr().err
    (tmp_path / "runs" / "a" / fault_runs.RUN_FILE).write_text("[1, 2]")
    assert evaluation.main(["report", str(tmp_path / "runs")]) == 2
    assert "Cannot make the report" in capsys.readouterr().err


# Rehearsals


def test_the_rehearse_command_on_the_false_alarm_saves_what_the_rules_saw_and_calls_no_model(monkeypatch, tmp_path):
    world = World()
    monkeypatch.setattr(evaluation.faults, "LocalStack", lambda: None)
    monkeypatch.setattr(evaluation.alerts, "Alerting", world.alerting)
    monkeypatch.setattr(evaluation.fault_runs, "Stack", lambda local, alerting: world.stack())
    monkeypatch.setattr(evaluation.fault_runs, "_provenance", lambda command: {"rerun_command": command})
    monkeypatch.setattr(evaluation.loop, "investigate_live", lambda *a, **k: pytest.fail("a model was called"))
    assert evaluation.main(["rehearse", "false-alarm", "--evidence-dir", str(tmp_path)]) == 0
    [folder] = tmp_path.iterdir()
    assert folder.name.endswith("_rehearsal_false-alarm")
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["rehearsal"] and saved["loop"] is None and saved["alert"]["name"] == "ConsumerLagHigh"
    assert "A rehearsal: no model was called" in saved["label"]


def test_the_rehearse_command_on_missing_deploys_rehearses_fault_1_then_probes_the_tools(monkeypatch, tmp_path):
    world = DockerStack()
    monkeypatch.setattr(evaluation.faults, "LocalStack", lambda: world.local)
    monkeypatch.setattr(evaluation.alerts, "Alerting", world.alerting)
    monkeypatch.setattr(evaluation.fault_runs, "Stack", lambda local, alerting: world.stack())
    monkeypatch.setattr(evaluation.fault_runs, "_provenance", lambda command: {"rerun_command": command})
    monkeypatch.setattr(evaluation, "McpClient", lambda: Served().client)
    real = evaluation.Withheld
    monkeypatch.setattr(evaluation, "Withheld", lambda inner, names: real(inner, names, sleep=lambda s: None))
    assert evaluation.main(["rehearse", "missing-deploys", "--evidence-dir", str(tmp_path)]) == 0
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    assert saved["rehearsal"] and saved["fault"]["name"] == "duplicate-delivery" and saved["alert"] is not None
    assert "withheld" not in saved["label"]  # no loop ran, so no call to it was made then
    probe = json.loads((folder / evaluation.PROBE_FILE).read_text())["results"]
    assert [(p["tool"], p["is_error"]) for p in probe] == [("recent_deploys", True), ("service_health", False)]
    assert probe[0]["text"] == evaluation.DOCKER_SILENT and world.docker.settings(LEDGER) == GOOD[LEDGER]


# The make targets


def test_the_evaluations_make_targets_pass_their_values_through_the_environment():
    for target, value, name in [
        ("eval", "MODEL=x' ; echo pwned", '"$MODEL"'),
        ("eval", "CASES=x' ; echo pwned", '"$CASES"'),
        ("eval", "SEEDS=x' ; echo pwned", '"$SEEDS"'),
        ("eval-report", "RUNS=x' ; echo pwned", "$RUNS"),
        ("rehearse-case", "CASE=x' ; echo pwned", '"$CASE"'),
        ("rehearse-case", "SEED=1' ; echo pwned", '"$SEED"'),
    ]:
        shown = subprocess.run(["make", "-n", target, value], cwd=REPO, capture_output=True, text=True, check=True)
        assert "pwned" not in shown.stdout and name in shown.stdout, target
