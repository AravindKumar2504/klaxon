"""End to end: each fault of klaxon.faults, injected on purpose into the live stack while traffic flows.

These reset the stack (its data is synthetic) and take a few minutes: make e2e. They run in order on one
stack: the two faults whose damage clears with them first, the one that leaves money booked twice last. The outage
runs twice (week 4, item 3): without the payments API's circuit breaker (the known-bad old behavior, deployed and
then rolled back), and with it, the default.
"""

import time
import uuid

import httpx
import pytest
from confluent_kafka import Consumer

from klaxon import faults, traffic
from klaxon.checks.live import NotSettled, check_live
from klaxon.demo import live
from klaxon.patient import stack
from klaxon.patient.services.kafka_io import CONFIRMER_GROUP, DLQ_TOPIC, EVENTS_TOPIC, LEDGER_GROUP, consumer_lag

pytestmark = pytest.mark.e2e

KAFKA = "127.0.0.1:9094"
PROMETHEUS = "http://127.0.0.1:9090"


@pytest.fixture(scope="module")
def fault_stack():
    stack.reset()
    stack.up("idempotent")
    yield faults.LocalStack()
    stack.reset()  # never leave a fault's damage behind


class Traffic:
    """The traffic generator, running for the length of a with-block."""

    def __init__(self, **settings) -> None:
        self.generator = traffic.TrafficGenerator(traffic.Settings(**settings))

    def __enter__(self):
        self.generator.start()
        return self.generator

    def __exit__(self, *exc) -> None:
        self.generator.close()
        self.generator.join()


def until(condition, timeout: float, every: float = 1.0):
    deadline = time.monotonic() + timeout
    while not (value := condition()):
        assert time.monotonic() < deadline, "it never happened"
        time.sleep(every)
    return value


def dead_letters() -> list[dict]:
    """Every message on the dead-letter topic: the consumer that refused it, where it came from, and why."""
    reader = Consumer(
        {"bootstrap.servers": KAFKA, "group.id": f"e2e-dlq-{uuid.uuid4().hex[:8]}", "auto.offset.reset": "earliest"}
    )
    reader.subscribe([DLQ_TOPIC])
    out, quiet_since = [], time.monotonic()
    try:
        while time.monotonic() - quiet_since < 3:
            msg = reader.poll(0.5)
            if msg is None or msg.error():
                continue
            quiet_since = time.monotonic()
            out.append({k: v.decode() for k, v in msg.headers()} | {"value": msg.value().decode(errors="replace")})
    finally:
        reader.close()
    return out


def ledger_behind() -> dict[int, int]:
    return consumer_lag(KAFKA, LEDGER_GROUP, EVENTS_TOPIC)


def test_a_poison_message_stops_the_retrying_ledger_and_the_rollback_drains_it(fault_stack):
    with Traffic(rate=5.0, seed=3):
        time.sleep(3)
        assert check_live().report.passed
        injected = faults.get("poison-message").inject(fault_stack)
        try:
            poison = injected["poison"]["event_id"]
            # One consumer reads all three partitions in one loop: stuck on the poison, it books nothing anywhere.
            behind = until(lambda: (lag := ledger_behind()) and min(lag.values()) >= 5 and lag, timeout=60)
            assert len(behind) == 3
            with pytest.raises(NotSettled) as stuck:
                check_live(timeout_seconds=5)
            assert stuck.value.settling.behind[LEDGER_GROUP] > 0 and stuck.value.settling.behind[CONFIRMER_GROUP] == 0
            # The confirmer, with the default policy, dead-lettered the same event as unreadable; the ledger never
            # did. (Maybe more than once: a lost acknowledgement makes the provider send it again.)
            letters = [d for d in dead_letters() if poison in d["value"]]
            assert letters and all(d["consumer"] == CONFIRMER_GROUP for d in letters)
            assert all(d["reason"].startswith("cannot confirm this event") for d in letters)
        finally:
            faults.get("poison-message").clear(fault_stack)
        result = check_live(timeout_seconds=60)  # rolled back: the poison is dead-lettered, the backlog drains
    assert result.report.passed, result.report
    assert {d["consumer"] for d in dead_letters() if poison in d["value"]} == {CONFIRMER_GROUP, LEDGER_GROUP}
    assert check_live(timeout_seconds=60).report.passed


def counted(query: str) -> float:
    response = httpx.get(f"{PROMETHEUS}/api/v1/query", params={"query": query}, timeout=10, trust_env=False)
    data = response.json()["data"]["result"]
    return float(data[0]["value"][1]) if data else 0.0


def counted_no_answers() -> float:
    return counted('sum(klaxon_provider_attempts_total{result="no_answer"})')


def counted_refusals() -> float:
    return counted("sum(klaxon_provider_refused_total)")


def timed(call) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    response = call()
    return response, time.monotonic() - started


def payer(api: httpx.Client):
    def pay(amount: int) -> httpx.Response:
        body = {"customer": "cus_e2e", "amount_cents": amount}
        return api.post("/payments", json=body, headers={"Idempotency-Key": f"e2e-outage-{uuid.uuid4().hex[:8]}"})

    return pay


def test_without_its_breaker_a_provider_outage_makes_payments_wait_through_their_timeouts(fault_stack):
    """The old behavior, kept as a known-bad setting (week 4, item 3): PROVIDER_BREAKER=off, deployed and logged."""
    fault_stack.redeploy("payments-api", {"PROVIDER_BREAKER": "off"})
    try:
        assert stack.container_env("payments-api")["PROVIDER_BREAKER"] == "off"
        with Traffic(rate=3.0, seed=4), httpx.Client(base_url=live.API_URL, timeout=30.0, trust_env=False) as api:
            pay = payer(api)
            time.sleep(3)
            faults.get("provider-outage").inject(fault_stack)
            try:
                held, waited = timed(lambda: pay(4_200))
                assert held.status_code == 503 and held.json()["error"]["code"] == "provider_unavailable"
                assert waited >= 9  # three attempts, each waiting out its 3 s timeout: no circuit breaker
                assert check_live(timeout_seconds=60).report.passed  # nothing was charged, so no money is wrong
                until(lambda: counted_no_answers() >= 3, timeout=60, every=2)  # and the timeouts show in the metrics
            finally:
                faults.get("provider-outage").clear(fault_stack)
            until(lambda: pay(4_201).status_code == 201, timeout=60)
        assert check_live(timeout_seconds=60).report.passed
    finally:
        fault_stack.redeploy("payments-api", {"PROVIDER_BREAKER": "on"})  # never leave the known-bad setting behind
    assert stack.container_env("payments-api")["PROVIDER_BREAKER"] == "on"


def test_with_its_breaker_a_provider_outage_fails_fast_keeps_the_api_healthy_and_moves_no_money(fault_stack):
    """Week 4, item 3: the default. The traffic's own calls fail first and open the breaker; from then on a payment
    is answered 503 at once, the provider is not called, and the API stays healthy. Once the outage ends, a trial
    call gets an answer and payments go through again."""
    assert stack.container_env("payments-api")["PROVIDER_BREAKER"] == "on"
    with Traffic(rate=3.0, seed=6), httpx.Client(base_url=live.API_URL, timeout=30.0, trust_env=False) as api:
        pay = payer(api)
        time.sleep(3)
        refused_before = counted_refusals()
        faults.get("provider-outage").inject(fault_stack)
        try:
            fast, seconds = until(lambda: (found := timed(lambda: pay(4_300)))[1] < 1.0 and found, timeout=90)
            assert fast.status_code == 503 and fast.json()["error"]["code"] == "provider_unavailable"
            assert fast.json()["payment"]["status"] == "pending"  # its outcome is unknown, as before: no money moved
            health, health_seconds = timed(lambda: api.get("/healthz"))
            assert health.status_code == 200 and health_seconds < 2  # its worker threads are free
            until(lambda: counted_refusals() > refused_before, timeout=60, every=2)  # refused calls, never sent
            logs = stack.compose("logs", "--no-log-prefix", "--since", "10m", "payments-api")
            assert "circuit breaker open: 5 provider calls failed in a row" in logs
            assert check_live(timeout_seconds=60).report.passed  # nothing was charged, so no money is wrong
        finally:
            faults.get("provider-outage").clear(fault_stack)
        until(lambda: pay(4_301).status_code == 201, timeout=60)  # a trial call got an answer: the breaker closed
    logs = stack.compose("logs", "--no-log-prefix", "--since", "10m", "payments-api")
    assert "circuit breaker closed: the provider answered the trial call" in logs
    assert check_live(timeout_seconds=60).report.passed


def test_a_deploy_without_dedupe_counts_payments_twice_and_the_rollback_stops_new_damage(fault_stack):
    def counted_twice() -> set[str]:
        evidence = check_live(timeout_seconds=60).report.invariant("posted_exactly_once").evidence
        assert all(item["expected"] == 1 and item["times_posted"] > 1 for item in evidence), evidence  # nothing else
        return {item["event_id"] for item in evidence}

    with Traffic(rate=5.0, seed=5, lose_ack_rate=0.3, lost_answer_rate=0.0):
        time.sleep(3)
        assert check_live().report.passed
        faults.get("duplicate-delivery").inject(fault_stack)
        try:
            damaged = until(counted_twice, timeout=60, every=3)
            result = check_live(timeout_seconds=60)
            assert result.report.money_off_cents > 0  # the books claim money the provider never had
            assert result.report.invariant("payments_match_provider").passed  # our payments are right, the books not
            assert result.report.invariant("refunds_match_provider").passed
        finally:
            faults.get("duplicate-delivery").clear(fault_stack)
        at_rollback = counted_twice()
        time.sleep(10)  # traffic, and its duplicates, keep coming
        after = counted_twice()
    assert damaged <= at_rollback
    assert after == at_rollback  # the rollback stops new damage; what was booked twice stays booked twice
