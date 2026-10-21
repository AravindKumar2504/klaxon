"""The fault catalog, offline: what each fault does to the stack, and what undoes it."""

import json

import httpx
import pytest

from klaxon import faults
from klaxon.faults import FAULTS, LocalStack
from klaxon.patient.deploys import DeployLog
from tests.fake_docker import NAIVE, FakeDocker


class FakeStack:
    """Records what a fault asks of the stack."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.noise = {"lose_ack_rate": 0.05, "fail_after_charge_rate": 0.02, "seed": 7, "outage_delay_seconds": 0.0}

    def redeploy(self, service, env):
        self.calls.append(("redeploy", service, dict(env)))

    def change_behavior(self, **changes):
        self.calls.append(("behavior", changes))
        self.noise |= changes
        return self.noise

    def send_malformed_event(self):
        self.calls.append(("malformed event",))
        return {"event_id": "evt_000042", "payment_id": "pay_000041"}


GOOD = {"LEDGER_HANDLER": "idempotent", "LEDGER_BAD_EVENTS": "dead_letter"}


def test_duplicate_delivery_deploys_the_naive_ledger_and_clearing_rolls_it_back():
    s = FakeStack()
    FAULTS["duplicate-delivery"].inject(s)
    FAULTS["duplicate-delivery"].clear(s)
    assert s.calls == [
        ("redeploy", "ledger-consumer", GOOD | {"LEDGER_HANDLER": "naive"}),
        ("redeploy", "ledger-consumer", GOOD),
    ]


def test_poison_message_deploys_the_retrying_ledger_first_then_sends_the_poison():
    """In the other order the old consumer would dead-letter the poison, and nothing would stall."""
    s = FakeStack()
    done = FAULTS["poison-message"].inject(s)
    FAULTS["poison-message"].clear(s)
    assert s.calls == [
        ("redeploy", "ledger-consumer", GOOD | {"LEDGER_BAD_EVENTS": "retry"}),
        ("malformed event",),
        ("redeploy", "ledger-consumer", GOOD),
    ]
    assert done["poison"]["event_id"] == "evt_000042"


def test_provider_outage_starts_and_ends_without_touching_the_traffics_noise():
    s = FakeStack()
    FAULTS["provider-outage"].inject(s)
    assert s.noise == {"lose_ack_rate": 0.05, "fail_after_charge_rate": 0.02, "seed": 7, "outage_delay_seconds": 5.0}
    FAULTS["provider-outage"].clear(s)
    assert s.noise["outage_delay_seconds"] == 0.0 and s.noise["lose_ack_rate"] == 0.05


def test_changing_the_providers_behavior_keeps_the_rest_of_it():
    put = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            put.append(json.loads(request.content))
            return httpx.Response(200, json=put[-1])
        return httpx.Response(200, json={"lose_ack_rate": 0.05, "seed": 7, "fail_after_charge_rate": 0.02})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8081")
    LocalStack(http=http, compose=None, ensure_local=None).change_behavior(outage_delay_seconds=5.0)
    assert put == [{"lose_ack_rate": 0.05, "seed": 7, "fail_after_charge_rate": 0.02, "outage_delay_seconds": 5.0}]


def test_a_redeploy_is_one_service_on_this_macs_project_only_and_is_logged():
    docker = FakeDocker()  # made up, in memory: tests/fake_docker.py
    s = LocalStack(deployer=docker.deployer())
    s.redeploy("ledger-consumer", NAIVE)
    assert docker.local_checks == 1  # docker is checked to point at this Mac first
    assert docker.ups() == [(("up", "--detach", "--no-deps", "--wait", "ledger-consumer"), NAIVE)]
    (record,) = DeployLog().records()  # a rollback can go back to what it replaced (klaxon.agent.actions)
    assert (record.service, record.by, record.settings, record.replaced_settings) == (
        "ledger-consumer",
        "klaxon.faults",
        NAIVE,
        GOOD,
    )
    s.redeploy("ledger-consumer", NAIVE)  # already running them: nothing done, nothing logged
    assert len(docker.ups()) == 1 and len(DeployLog().records()) == 1


def test_a_local_stack_deploys_through_the_docker_it_was_given():
    def compose(*args, env=None):
        raise AssertionError("not called")

    def ensure_local():
        raise AssertionError("not called")

    def inspect(*ids):
        raise AssertionError("not called")

    s = LocalStack(compose=compose, ensure_local=ensure_local, inspect=inspect)
    assert (s.deployer._compose, s.deployer._ensure_local, s.deployer._inspect) == (compose, ensure_local, inspect)


@pytest.mark.parametrize("url", ["http://10.0.0.5:8081", "https://provider.example.com"])
def test_faults_only_ever_go_to_this_mac(url):
    with pytest.raises(ValueError, match="only this Mac"):
        LocalStack(provider_url=url)
    with pytest.raises(ValueError, match="only this Mac"):
        LocalStack(http=httpx.Client(base_url=url))
    assert LocalStack().http.trust_env is False  # and never through a proxy


class NoPayments(FakeStack):
    def send_malformed_event(self):
        self.calls.append(("malformed event",))
        refused = httpx.Response(409, request=httpx.Request("POST", "http://127.0.0.1:8081/admin/malformed-event"))
        raise httpx.HTTPStatusError("409 no_payment", request=refused.request, response=refused)


def test_a_poison_that_cannot_be_sent_rolls_its_deploy_back():
    """Never half a fault: without a payment to name, the poison is refused, and the retrying deploy is undone."""
    s = NoPayments()
    with pytest.raises(httpx.HTTPStatusError):
        FAULTS["poison-message"].inject(s)
    assert s.calls == [
        ("redeploy", "ledger-consumer", GOOD | {"LEDGER_BAD_EVENTS": "retry"}),
        ("malformed event",),
        ("redeploy", "ledger-consumer", GOOD),
    ]


def test_each_fault_says_what_the_money_check_should_show_in_each_phase():
    assert {name: fault.expected_money for name, fault in FAULTS.items()} == {
        "duplicate-delivery": {"during": "failed", "after": "failed", "quiet": "failed"},  # the damage stays
        "poison-message": {"during": "could not run", "after": "passed", "quiet": "passed"},
        "provider-outage": {"during": "passed", "after": "passed", "quiet": "passed"},  # nothing was charged
    }


def test_every_fault_names_its_cause_its_symptoms_and_its_remedy():
    for fault in FAULTS.values():
        assert fault.cause and fault.symptoms and fault.remedy and fault.title
        assert fault.describe()["name"] == fault.name
    assert set(FAULTS) == {"duplicate-delivery", "poison-message", "provider-outage"}
    with pytest.raises(KeyError, match="the faults are"):
        faults.get("disk-full")


def test_the_cli_lists_every_fault(capsys):
    assert faults.main(["list"]) == 0
    out = capsys.readouterr().out
    assert all(f"{name}:" in out for name in FAULTS)
    assert "  expected: " in out and "symptom:" not in out  # what a fault should do, until a run shows it


def test_the_stack_comes_up_with_dead_lettering_whatever_this_shells_variables_say(monkeypatch):
    """The known-bad "retry forever" setting must only ever come from a fault, never from a stray variable."""
    from klaxon.patient import stack

    seen = []
    monkeypatch.setenv("LEDGER_BAD_EVENTS", "retry")
    monkeypatch.setattr(stack, "ensure_local", lambda: None)
    monkeypatch.setattr(stack, "compose", lambda *args, env: seen.append(env))
    stack.up("idempotent")
    assert seen[0]["LEDGER_BAD_EVENTS"] == seen[0]["CONFIRMER_BAD_EVENTS"] == "dead_letter"
    makefile = (stack.REPO / "Makefile").read_text()
    assert (
        "export LEDGER_BAD_EVENTS := dead_letter" in makefile
        and "export CONFIRMER_BAD_EVENTS := dead_letter" in makefile
    )


def test_the_stack_comes_up_with_its_circuit_breaker_whatever_this_shells_variables_say(monkeypatch):
    """Week 4, item 3: the payments API without its breaker, the known-bad old behavior, only ever comes from a
    deploy, never from a stray variable."""
    from klaxon.patient import stack

    seen = []
    monkeypatch.setenv("PROVIDER_BREAKER", "off")
    monkeypatch.setattr(stack, "ensure_local", lambda: None)
    monkeypatch.setattr(stack, "compose", lambda *args, env: seen.append(env))
    stack.up("idempotent")
    assert seen[0]["PROVIDER_BREAKER"] == "on"
    assert "export PROVIDER_BREAKER := on" in (stack.REPO / "Makefile").read_text()


def test_fault_3_version_2_names_the_breaker_and_keeps_what_version_1_recorded():
    """Week 4, item 3. Version 1's cause and remedy are the ones the week 3 run on fault 3 was compared with."""
    from klaxon.patient.services.provider_client import BREAKER_FAILURES, BREAKER_OPEN_SECONDS
    from klaxon.patient.stack import REPO

    outage = FAULTS["provider-outage"]
    assert f"after {BREAKER_FAILURES} calls in a row got no answer" in outage.cause
    assert f"one trial call through {BREAKER_OPEN_SECONDS:g} s after it opened" in outage.cause
    assert outage.remedy.startswith("nothing to change on our side") and outage.describe()["cause"] == outage.cause
    saved = json.loads((REPO / "evidence/week3/2026-10-19T024001Z_fault-run_provider-outage/run.json").read_text())
    then = outage.recorded(1)
    assert (then.cause, then.remedy) == (saved["comparison"]["recorded_cause"], saved["comparison"]["remedy"])
    assert outage.recorded(2) == faults.Recorded(outage.cause, outage.remedy)
    for name in ("duplicate-delivery", "poison-message"):  # unchanged
        assert (
            FAULTS[name].recorded(1)
            == FAULTS[name].recorded(2)
            == faults.Recorded(FAULTS[name].cause, FAULTS[name].remedy)
        )


def test_clearing_a_fault_starts_a_stopped_ledger_with_its_good_settings():
    """The harness clears every fault in a finally: that must work even when the bad deploy left nothing running."""
    docker = FakeDocker()
    del docker.containers["ledger-consumer"]
    FAULTS["duplicate-delivery"].clear(LocalStack(deployer=docker.deployer()))
    assert docker.settings("ledger-consumer") == GOOD
