"""End to end: each fault of klaxon.faults, injected on purpose into the live stack while traffic flows.

These reset the stack (its data is synthetic) and take a few minutes: make e2e. They run in order on one
stack: the two faults whose damage clears with them first, the one that leaves money booked twice last.
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


def counted_no_answers() -> float:
    query = 'sum(klaxon_provider_attempts_total{result="no_answer"})'
    response = httpx.get(f"{PROMETHEUS}/api/v1/query", params={"query": query}, timeout=10, trust_env=False)
    data = response.json()["data"]["result"]
    return float(data[0]["value"][1]) if data else 0.0


def test_a_provider_outage_makes_payments_wait_through_their_timeouts_and_moves_no_money(fault_stack):
    with Traffic(rate=3.0, seed=4), httpx.Client(base_url=live.API_URL, timeout=30.0, trust_env=False) as api:

        def pay(amount: int) -> httpx.Response:
            body = {"customer": "cus_e2e", "amount_cents": amount}
            return api.post("/payments", json=body, headers={"Idempotency-Key": f"e2e-outage-{uuid.uuid4().hex[:8]}"})

        time.sleep(3)
        faults.get("provider-outage").inject(fault_stack)
        try:
            started = time.monotonic()
            held = pay(4_200)
            waited = time.monotonic() - started
            assert held.status_code == 503 and held.json()["error"]["code"] == "provider_unavailable"
            assert waited >= 9  # three attempts, each waiting out its 3 s timeout: no circuit breaker
            assert check_live(timeout_seconds=60).report.passed  # nothing was charged, so no money is wrong
            until(lambda: counted_no_answers() >= 3, timeout=60, every=2)  # and the timeouts show in the metrics
        finally:
            faults.get("provider-outage").clear(fault_stack)
        until(lambda: pay(4_201).status_code == 201, timeout=60)
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
        finally:
            faults.get("duplicate-delivery").clear(fault_stack)
        at_rollback = counted_twice()
        time.sleep(10)  # traffic, and its duplicates, keep coming
        after = counted_twice()
    assert damaged <= at_rollback
    assert after == at_rollback  # the rollback stops new damage; what was booked twice stays booked twice
