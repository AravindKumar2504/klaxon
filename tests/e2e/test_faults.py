"""End to end: each fault of klaxon.faults, injected on purpose into the live stack while traffic flows.

These reset the stack (its data is synthetic) and take a few minutes: make e2e. They run in order on one
stack: the faults whose damage clears with them first, the two that leave damage last: money booked twice, then
customers charged 100 times the amount. The outage runs twice (week 4, item 3): without the payments API's circuit
breaker (the known-bad old behavior, deployed and then rolled back), and with it, the default. Faults 4 to 6 came in
week 4, item 4.
"""

import time
import uuid

import httpx
import pytest
from confluent_kafka import Consumer

from klaxon import alerts, faults, traffic
from klaxon.checks.live import PROVIDER_URL, NotSettled, check_live
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


def payments_api_logs() -> str:
    """The running payments API container's own lines: a redeploy starts a new container, and a new log."""
    return stack.compose("logs", "--no-log-prefix", "payments-api")


def payments_api_health() -> str:
    [row] = [row for row in stack.containers() if row.get("Service") == "payments-api"]
    return row.get("Health") or row.get("State") or "unknown"


def test_without_its_breaker_a_provider_outage_makes_payments_wait_through_their_timeouts(fault_stack):
    """The old behavior, kept as a known-bad setting (week 4, item 3): PROVIDER_BREAKER=off, deployed and logged,
    then rolled back. A breaker would have opened about 11 s in; without one, a payment 15 s in still waits."""
    try:
        fault_stack.redeploy("payments-api", faults.GOOD_API | {"PROVIDER_BREAKER": "off"})
        assert stack.container_env("payments-api")["PROVIDER_BREAKER"] == "off"
        with Traffic(rate=3.0, seed=4), httpx.Client(base_url=live.API_URL, timeout=60.0, trust_env=False) as api:
            pay = payer(api)
            time.sleep(3)
            faults.get("provider-outage").inject(fault_stack)
            injected = time.monotonic()
            try:
                held, waited = timed(lambda: pay(4_200))
                assert held.status_code == 503 and held.json()["error"]["code"] == "provider_unavailable"
                assert waited >= 9  # three attempts, each waiting out its 3 s timeout
                time.sleep(max(0.0, 15 - (time.monotonic() - injected)))  # past the time a breaker would have opened
                later, waited_later = timed(lambda: pay(4_202))
                assert later.status_code == 503 and waited_later >= 9  # still waiting: no circuit breaker
                assert check_live(timeout_seconds=60).report.passed  # nothing was charged, so no money is wrong
                until(lambda: counted_no_answers() >= 3, timeout=60, every=2)  # and the timeouts show in the metrics
            finally:
                faults.get("provider-outage").clear(fault_stack)
            until(lambda: pay(4_201).status_code == 201, timeout=60)
        assert check_live(timeout_seconds=60).report.passed
        assert "circuit breaker" not in payments_api_logs()
    finally:
        fault_stack.redeploy("payments-api", faults.GOOD_API)  # never leave the known-bad setting behind
    assert stack.container_env("payments-api")["PROVIDER_BREAKER"] == "on"


def test_with_its_breaker_a_provider_outage_fails_fast_keeps_the_api_healthy_and_moves_no_money(fault_stack):
    """Week 4, item 3: the default. The traffic's own calls fail first and open the breaker; from then on a payment
    is answered 503 at once, the provider is not called, and the API stays healthy. Once the outage ends, a trial
    call gets an answer and payments go through again."""
    assert stack.container_env("payments-api")["PROVIDER_BREAKER"] == "on"
    with Traffic(rate=3.0, seed=6), httpx.Client(base_url=live.API_URL, timeout=60.0, trust_env=False) as api:
        pay = payer(api)
        time.sleep(3)
        refused_before = counted_refusals()
        faults.get("provider-outage").inject(fault_stack)
        try:
            fast, seconds = until(lambda: (found := timed(lambda: pay(4_300)))[1] < 1.0 and found, timeout=90)
            assert fast.status_code == 503 and fast.json()["error"]["code"] == "provider_unavailable"
            assert fast.json()["payment"]["status"] == "pending"  # its outcome is unknown, as before: no money moved
            until(lambda: (health := timed(lambda: api.get("/healthz")))[0].status_code == 200 and health[1] < 1.0, 30)
            for _ in range(6):  # half a minute into the outage, docker's own health check still passes
                assert payments_api_health() == "healthy"
                time.sleep(5)
            until(lambda: counted_refusals() > refused_before, timeout=60, every=2)  # refused calls, counted
            assert "circuit breaker open: 5 provider calls in a row got no answer" in payments_api_logs()
            assert check_live(timeout_seconds=60).report.passed  # nothing was charged, so no money is wrong
        finally:
            faults.get("provider-outage").clear(fault_stack)
        until(lambda: pay(4_301).status_code == 201, timeout=60)  # a trial call got an answer: the breaker closed
    assert "circuit breaker closed: the provider answered the trial call" in payments_api_logs()
    assert check_live(timeout_seconds=60).report.passed


def receiver_logs() -> str:
    return stack.compose("logs", "--no-log-prefix", "webhook-receiver")


def counted_bad_signatures() -> float:
    return counted('sum(klaxon_webhooks_received_total{result="bad_signature"})')


def test_a_receiver_on_a_secret_the_provider_does_not_sign_with_stops_the_books_until_it_is_rolled_back(fault_stack):
    """Week 4, item 4, fault 5: every webhook is refused, nothing reaches Kafka, and the books cannot catch up, which
    only the books rule sees. Once rolled back, the provider sends every refused webhook again, and the books catch
    up by themselves."""
    with Traffic(rate=3.0, seed=8):
        time.sleep(3)
        assert check_live().report.passed
        refused_before = counted_bad_signatures()
        faults.get("secret-rotation").inject(fault_stack)
        try:
            assert stack.container_env("webhook-receiver")["WEBHOOK_SECRET_VERSION"] == "v2"
            with pytest.raises(NotSettled) as stuck:
                check_live(timeout_seconds=10)
            assert stuck.value.settling.behind is None and stuck.value.settling.pending_deliveries > 0
            until(lambda: counted_bad_signatures() > refused_before, timeout=60, every=2)
            assert "webhook refused: bad signature" in receiver_logs()
            # The books rule fires at its second evaluation. The rules' one-minute windows may still hold the errors of
            # the outage test before this one (seen on 2026-10-24), so the alert may carry them too, and their name.
            rules = alerts.Alerting()
            first, second = rules.evaluate(), rules.evaluate()
            [books] = [check for check in second.checks if check.rule == "BooksBehindProvider"]
            assert books.firing and not first.checks[-1].firing and second.alert is not None
            assert "of the provider's webhooks up to it had not been acknowledged yet" in second.alert.summary
            assert not [check for check in second.checks if check.rule == "MoneyCheckFailed" and check.breached]
        finally:
            faults.get("secret-rotation").clear(fault_stack)
        result = check_live(timeout_seconds=90)  # each refused webhook is sent again within a minute of its last try
    assert result.report.passed, result.report
    assert stack.container_env("webhook-receiver")["WEBHOOK_SECRET_VERSION"] == "v1"


def test_a_deploy_that_keeps_its_refund_connections_runs_the_api_out_of_them_until_it_is_rolled_back(fault_stack):
    """Week 4, item 4, fault 6: each refund keeps a connection of the payments API's pool of 10. Once none is left,
    every request waits 5 s for one and fails, and the API's health check fails, while PostgreSQL is up. Nothing is
    charged wrong. Rolled back, its new container has a full pool."""
    with Traffic(rate=3.0, seed=9), httpx.Client(base_url=live.API_URL, timeout=60.0, trust_env=False) as api:
        pay = payer(api)
        time.sleep(3)
        faults.get("pool-exhaustion").inject(fault_stack)
        try:
            assert stack.container_env("payments-api")["REFUND_CONNECTION"] == "kept"
            failed, waited = until(lambda: (found := timed(lambda: pay(4_400)))[0].status_code == 500 and found, 120)
            assert waited >= 4.5  # it waited out the pool's 5 s timeout
            assert "PoolTimeout" in payments_api_logs()
            until(lambda: payments_api_health() == "unhealthy", timeout=60, every=5)
            assert check_live(timeout_seconds=60).report.passed  # a payment that got no connection was never charged
        finally:
            faults.get("pool-exhaustion").clear(fault_stack)
        until(lambda: pay(4_401).status_code == 201, timeout=60)
        assert payments_api_health() == "healthy"
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


def provider_charge(provider_payment_id: str) -> dict:
    response = httpx.get(f"{PROVIDER_URL}/v1/payments/{provider_payment_id}", timeout=10, trust_env=False)
    return response.raise_for_status().json()


def test_a_deploy_with_the_money_unit_bug_charges_100_times_the_amount_and_the_rollback_stops_new_damage(fault_stack):
    """Week 4, item 4, fault 4, last: its overcharges stay. Our records and our answers keep the right amount, the
    ledger books what the provider charged, and only the fourth invariant sees it. A rollback stops new damage; each
    overcharge stays until a person refunds it."""

    def overcharged() -> dict[str, dict]:
        evidence = check_live(timeout_seconds=60).report.invariant("payments_match_provider").evidence
        return {item["payment_id"]: item for item in evidence if "amount differs" in item["problem"]}

    with Traffic(rate=3.0, seed=10), httpx.Client(base_url=live.API_URL, timeout=60.0, trust_env=False) as api:
        pay = payer(api)
        time.sleep(3)
        assert overcharged() == {}  # the earlier tests' damage is in the books, not in our payments
        faults.get("money-unit").inject(fault_stack)
        try:
            assert stack.container_env("payments-api")["CHARGE_AMOUNT_UNIT"] == "dollars"
            # The traffic's noise loses 2% of the provider's answers, the test's own included: pay until one is not.
            payment = until(lambda: (paid := pay(4_500)).status_code == 201 and paid, timeout=30).json()
            assert (payment["status"], payment["amount_cents"]) == ("succeeded", 4_500)
            assert provider_charge(payment["provider_payment_id"])["amount_cents"] == 450_000
            found = until(lambda: (seen := overcharged()) and payment["id"] in seen and seen, timeout=60, every=3)
            for item in found.values():
                assert [charge["amount_cents"] for charge in item["provider_charges"]] == [100 * item["amount_cents"]]
            # The ledger booked what the provider charged, so it agrees with the provider about these charges (the
            # duplicate-delivery test before this one leaves other payments' books wrong).
            charged = {charge["id"] for item in found.values() for charge in item["provider_charges"]}
            books_off = {
                item["scope"] for item in check_live(timeout_seconds=60).report.invariant("matches_provider").evidence
            }
            assert not charged & books_off
        finally:
            faults.get("money-unit").clear(fault_stack)
        at_rollback = set(overcharged())
        after = until(lambda: (paid := pay(4_501)).status_code == 201 and paid, timeout=30).json()
        assert provider_charge(after["provider_payment_id"])["amount_cents"] == 4_501
        time.sleep(10)
        still = set(overcharged())
    assert still == at_rollback  # no new overcharge; the ones before the rollback stay
    assert stack.container_env("payments-api")["CHARGE_AMOUNT_UNIT"] == "cents"
