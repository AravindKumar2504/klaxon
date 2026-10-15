"""The fault scenario runner, offline: it always clears the fault, and it says when a run did not go as expected."""

import json

import httpx
import pytest

from klaxon import faults
from klaxon.demo import fault_scenario
from klaxon.demo.fault_scenario import FaultRun, Moment, MoneyView, render, unhealthy_containers
from klaxon.traffic import Settings


class FakeGenerator:
    def __init__(self, settings) -> None:
        self.settings, self.calls = settings, []
        self.stats = type("Stats", (), {"to_dict": lambda self: {"counts": {}, "unexpected": []}})()
        self.behavior_note = "the provider's behavior was not changed"

    def start(self):
        self.calls.append("start")

    def close(self):
        self.calls.append("close")

    def join(self):
        self.calls.append("join")


@pytest.fixture
def runner(monkeypatch):
    """run_live with everything outside replaced: the fault, the traffic, the samples and the money check."""
    calls, generators = [], []

    def generator(settings):
        generators.append(FakeGenerator(settings))
        return generators[-1]

    fault = faults.Fault(
        name="test-fault",
        title="a test fault",
        cause="a test",
        symptoms=("none",),
        remedy="none",
        expected_money={"during": "failed", "after": "passed", "quiet": "passed"},
        inject=lambda s: calls.append("inject") or {"injected": True},
        clear=lambda s: calls.append("clear") or {"cleared": True},
    )
    monkeypatch.setattr(fault_scenario.faults, "get", lambda name: fault)
    monkeypatch.setattr(fault_scenario.traffic, "TrafficGenerator", generator)
    monkeypatch.setattr(fault_scenario.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(fault_scenario, "sample", lambda phase, seconds, http: Moment(phase, seconds, "now", [], {}))
    said = {"before": "passed", "during": "failed", "after": "passed", "quiet": "passed"}
    monkeypatch.setattr(fault_scenario, "money_view", lambda phase, **kwargs: MoneyView(phase, said[phase], []))
    return calls, generators, said


def test_a_run_that_goes_as_expected(runner):
    calls, generators, _ = runner
    run = fault_scenario.run_live("test-fault", during_seconds=0, after_seconds=0)
    assert calls == ["inject", "clear"] and generators[0].calls == ["start", "close", "join"]
    assert run.as_expected and run.surprises() == []
    assert render(run).splitlines()[-1] == "RESULT: the money check said what the fault expects in every phase"


def test_a_run_whose_money_check_says_otherwise_is_not_as_expected(runner):
    _, _, said = runner
    said["after"] = "failed"  # a rollback that did not recover
    run = fault_scenario.run_live("test-fault", during_seconds=0, after_seconds=0)
    assert run.surprises() == ["after: expected passed, got failed"]
    assert render(run).splitlines()[-1] == "RESULT: NOT as expected: after: expected passed, got failed"


def test_the_fault_is_cleared_and_the_traffic_stopped_even_when_watching_fails(runner, monkeypatch):
    calls, generators, _ = runner

    def broken_watch(phase, *args):
        raise RuntimeError("Prometheus went away")

    monkeypatch.setattr(fault_scenario, "watch", broken_watch)
    with pytest.raises(RuntimeError, match="Prometheus went away"):
        fault_scenario.run_live("test-fault")
    assert calls == ["inject", "clear"]  # never left in place
    assert generators[0].calls == ["start", "close", "join"]


def test_no_fault_is_injected_when_the_money_check_fails_before_it(runner):
    calls, generators, said = runner
    said["before"] = "failed"
    with pytest.raises(fault_scenario.DemoError, match="does not pass before the fault"):
        fault_scenario.run_live("test-fault")
    assert calls == [] and generators[0].calls == ["start", "close", "join"]


@pytest.mark.parametrize("as_array", [True, False])
def test_unhealthy_containers_come_from_either_compose_ps_format(monkeypatch, as_array):
    rows = [
        {"Service": "postgres", "State": "running", "Health": "healthy"},
        {"Service": "ledger-consumer", "State": "running", "Health": "unhealthy"},
        {"Service": "payments-api", "State": "exited", "Health": ""},
        {"Service": "kafka-init", "State": "exited", "Health": ""},  # one-shot: exiting is its job
        {"Service": "redis", "State": "running", "Health": ""},
    ]
    text = json.dumps(rows) if as_array else "\n".join(json.dumps(row) for row in rows)
    monkeypatch.setattr(fault_scenario.stack, "compose", lambda *args: text)
    assert unhealthy_containers() == ["ledger-consumer (unhealthy)", "payments-api (exited)"]


def test_prometheus_answers_that_are_not_numbers_are_kept_as_unknown():
    def handler(request):
        result = [
            {"metric": {"a": "1"}, "value": [0, "NaN"]},
            {"metric": {}, "value": [0, "+Inf"]},
            {"metric": {}, "value": [0, "2.5"]},
        ]
        return httpx.Response(200, json={"data": {"result": result}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert [point["value"] for point in fault_scenario.prometheus("q", http)] == [None, None, 2.5]


def moment(phase, dead_letters):
    return Moment(phase, 0.0, "now", [], {"dead_letters": [{"labels": {"consumer": "ledger"}, "value": dead_letters}]})


def test_dead_letters_are_shown_as_new_since_the_first_sample_not_as_lifetime_totals():
    """A stack that ran faults before already has dead letters; the run must not count them as its own."""
    run = FaultRun(
        fault=faults.FAULTS["poison-message"].describe(),
        settings=Settings(run_id="r1"),
        timings={"injected_at_seconds": 3.1, "cleared_at_seconds": 60.0},
        injected={},
        cleared={},
        moments=[moment("before", 4.0), moment("after", 5.0)],
        money=[
            MoneyView("during", "could not run", ["not settled"], without_waiting=MoneyView("during", "failed", []))
        ],
        traffic={"counts": {}},
        behavior_note="",
    )
    out = render(run)
    assert "new dead letters: ledger 0," in out and "new dead letters: ledger 1," in out
    assert "events still in flight count as lost too" in out
    assert "Rates are over the last minute" in out and "injected (took 3.1 s)" in out
