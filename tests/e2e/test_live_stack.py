"""End to end: the whole Compose stack, driven over HTTP the way a client and an operator would.

These reset the stack (its data is synthetic) and take about a minute and a half: make e2e.
The tests in this file run in order and share one stack, except the last two, which build their own.
"""

import threading
import time

import httpx
import pytest

from klaxon.checks.live import PROVIDER_URL, ProviderAPI, check_live
from klaxon.demo import live, lost_answer
from klaxon.patient import stack
from klaxon.patient.provider import PAYMENT_SUCCEEDED
from klaxon.patient.services.kafka_io import CONFIRMER_GROUP, EVENTS_TOPIC, LEDGER_GROUP, consumer_lag

pytestmark = pytest.mark.e2e

API_URL = live.API_URL
KAFKA = "127.0.0.1:9094"


@pytest.fixture(scope="module")
def idempotent_stack():
    stack.reset()
    stack.up("idempotent")
    yield


@pytest.fixture
def api():
    with httpx.Client(base_url=API_URL, timeout=20.0) as client:
        yield client


def pay(api, key, amount, customer="cus_e2e"):
    return api.post("/payments", json={"customer": customer, "amount_cents": amount}, headers={"Idempotency-Key": key})


def charges() -> int:
    """How many payments the provider says it captured: the count that must never go up on a retry."""
    return sum(1 for e in ProviderAPI(PROVIDER_URL).events if e.type == PAYMENT_SUCCEEDED)


def ledger_transactions() -> int:
    ((n,),) = live._readonly_rows("SELECT count(*) FROM transactions")
    return n


def ledger_transactions_if_reachable() -> int | None:
    try:
        return ledger_transactions()
    except Exception:
        return None


def test_the_first_pieces_scenario_passes_with_the_idempotent_consumer(idempotent_stack):
    run = live.run_live("idempotent")
    assert run.report.passed, live.render(run)
    assert (run.dashboard.requests, run.dashboard.status) == (7, "GREEN")
    assert run.delivery_stats["duplicate_deliveries"] == 1
    assert len(run.ledger_transactions) == 6  # the duplicate delivery was not booked


def set_behavior(**behavior) -> None:
    with httpx.Client(base_url=PROVIDER_URL) as provider:
        provider.put("/admin/behavior", json=behavior).raise_for_status()


def test_client_retries_and_lost_acknowledgements_still_book_every_payment_once(idempotent_stack, api):
    before = charges()
    set_behavior(lose_ack_rate=0.3, seed=11)
    try:
        ids = []
        for n in range(1, 21):
            first = pay(api, f"e2e-pay-{n}", 1_000 + n)
            retry = pay(api, f"e2e-pay-{n}", 1_000 + n)  # the client did not hear back, so it sends it again
            assert first.status_code == retry.status_code == 201
            assert retry.headers["idempotent-replayed"] == "true" and retry.json() == first.json()
            ids.append(first.json()["id"])
        for n, payment_id in enumerate(ids[:5], start=1):
            response = api.post(
                f"/payments/{payment_id}/refunds",
                json={"amount_cents": 500},
                headers={"Idempotency-Key": f"e2e-refund-{n}"},
            )
            assert response.status_code == 201
        result = check_live()
        with httpx.Client(base_url=PROVIDER_URL) as provider:
            stats = provider.get("/admin/deliveries").json()["stats"]
    finally:
        set_behavior()  # the later tests share this stack: never leave it losing acknowledgements
    assert result.report.passed, result.report
    assert charges() == before + 20  # 40 requests, 20 payments
    assert stats["duplicate_deliveries"] > 1  # the rate really did cause duplicates


def test_concurrent_requests_with_one_key_charge_once(idempotent_stack):
    """Eight copies of one request at the same moment, against the real Redis and PostgreSQL."""
    before = charges()
    start = threading.Barrier(8)
    answers: list[httpx.Response] = []

    def send():
        with httpx.Client(base_url=API_URL, timeout=20.0) as client:
            start.wait()
            answers.append(pay(client, "e2e-concurrent", 3_333))

    threads = [threading.Thread(target=send) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statuses = sorted(answer.status_code for answer in answers)
    assert set(statuses) <= {201, 409} and 201 in statuses
    assert len({answer.json()["id"] for answer in answers if answer.status_code == 201}) == 1
    with httpx.Client(base_url=API_URL, timeout=20.0) as client:
        assert pay(client, "e2e-concurrent", 3_333).status_code == 201  # finished now, so a repeat is a replay
    assert charges() == before + 1
    assert check_live().report.passed


def test_a_charge_whose_answer_is_lost_is_confirmed_by_its_webhook(idempotent_stack, api):
    """The provider saves the charge and answers 500 to every retry. Only the webhook can tell us it went through."""
    before = charges()
    run = lost_answer.run_live("by_our_id")
    assert run.report.passed, lost_answer.render(run)
    lost = run.requests[run.lost_position - 1]
    assert (lost["status"], lost["body"]["payment"]["status"]) == (503, "pending")  # the client heard "unknown"
    [charge] = [p for p in run.provider.payments if p.klaxon_payment_id == run.stuck_payment_id]
    assert (run.after_webhooks["status"], run.after_webhooks["provider_payment_id"]) == ("succeeded", charge.id)
    assert run.refund["status"] == 201  # a confirmed payment can be refunded
    # A client that does retry later gets the confirmed payment, and the provider charges nothing new.
    payment = lost["body"]["payment"]
    retry = pay(api, lost["idempotency_key"], payment["amount_cents"], customer=payment["customer"])
    assert retry.status_code == 201 and retry.headers["idempotent-replayed"] == "true"
    assert (retry.json()["id"], retry.json()["status"]) == (run.stuck_payment_id, "succeeded")
    assert charges() == before + 5
    assert check_live().report.passed


def test_with_redis_stopped_a_retry_still_cannot_charge_twice(idempotent_stack, api):
    before = charges()
    stack.compose("stop", "redis")
    try:
        first = pay(api, "e2e-redis-down", 4_242)
        retry = pay(api, "e2e-redis-down", 4_242)
        health = api.get("/healthz").json()
    finally:
        stack.compose("up", "--detach", "--wait", "--no-deps", "redis")
    assert first.status_code == retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"] and retry.headers["idempotent-replayed"] == "true"
    assert health["status"] == "degraded: redis unreachable"
    assert charges() == before + 1
    assert check_live().report.passed


def payment_rows() -> list[tuple]:
    return live._readonly_rows("SELECT id, status, provider_payment_id, updated_at FROM payments ORDER BY id")


def test_replaying_the_whole_topic_from_the_start_changes_nothing_for_either_consumer(idempotent_stack):
    assert check_live().report.passed
    booked, payments = ledger_transactions(), payment_rows()
    # A clean stop leaves the consumer group, so its offsets can be reset.
    stack.compose("stop", "ledger-consumer", "payment-confirmer")
    for group in (LEDGER_GROUP, CONFIRMER_GROUP):
        for _ in range(15):
            try:
                stack.compose(
                    "exec", "-T", "kafka", "/opt/kafka/bin/kafka-consumer-groups.sh", "--bootstrap-server",
                    "localhost:9092", "--group", group, "--topic", EVENTS_TOPIC, "--reset-offsets",
                    "--to-earliest", "--execute",
                )  # fmt: skip
                break
            except stack.StackError:
                time.sleep(1)  # the group can take a moment to become empty
        else:
            pytest.fail(f"could not reset the {group} group's offsets")
        replay = sum(consumer_lag(KAFKA, group, EVENTS_TOPIC).values())
        assert replay > 30  # every message is due again: what a crash before every commit would look like

    stack.start_container("ledger-consumer")
    stack.start_container("payment-confirmer")
    result = check_live(timeout_seconds=90)
    assert result.report.passed, result.report
    assert ledger_transactions() == booked
    assert payment_rows() == payments  # not even an updated_at moved: a confirmed payment is never written again


def test_with_kafka_down_webhooks_wait_at_the_provider_and_are_booked_once_it_returns(idempotent_stack, api):
    booked = ledger_transactions()
    stack.compose("stop", "kafka")
    try:
        response = pay(api, "e2e-kafka-down", 7_777)
        assert response.status_code == 201  # taking a payment does not need Kafka
        time.sleep(3)  # let the provider try, and fail, to deliver
        with httpx.Client(base_url=PROVIDER_URL) as provider:
            stats = provider.get("/admin/deliveries").json()["stats"]
        assert stats["pending"] >= 1  # not acknowledged, so the provider keeps it
    finally:
        stack.compose("up", "--detach", "--wait", "--no-deps", "kafka")
    result = check_live(timeout_seconds=120)
    assert result.report.passed, result.report
    assert ledger_transactions() == booked + 1


def test_with_postgres_down_both_consumers_wait_and_catch_up_after(idempotent_stack, api):
    booked = ledger_transactions()
    stack.compose("stop", "ledger-consumer", "payment-confirmer")
    for n in range(1, 3):
        assert pay(api, f"e2e-pg-down-{n}", 500 + n).status_code == 201  # events pile up in Kafka
    set_behavior(fail_after_charge_rate=1.0)
    try:
        lost = pay(api, "e2e-pg-down-3", 503)
    finally:
        set_behavior()
    assert lost.status_code == 503  # its answer was lost: only the confirmer can settle it now
    stuck = lost.json()["payment"]["id"]
    stack.compose("stop", "postgres")
    try:
        # Both start with the database down; neither may skip anything.
        stack.start_container("ledger-consumer")
        stack.start_container("payment-confirmer")
        deadline = time.monotonic() + 45
        for service, verb in (("ledger-consumer", "book"), ("payment-confirmer", "confirm")):
            while f"could not {verb}; will retry the same message" not in stack.compose("logs", service):
                assert time.monotonic() < deadline, f"{service} never tried, and failed, to {verb}"
                time.sleep(1)
        assert ledger_transactions_if_reachable() is None  # really down
    finally:
        stack.compose("up", "--detach", "--wait", "--no-deps", "postgres")
    result = check_live(timeout_seconds=120)
    assert result.report.passed, result.report
    assert ledger_transactions() == booked + 3
    assert api.get(f"/payments/{stuck}").json()["status"] == "succeeded"


@pytest.fixture(scope="module")
def naive_stack():
    stack.reset()
    stack.up("naive")
    yield
    stack.reset()  # do not leave the known-bad baseline running


def test_the_naive_consumer_is_caught_by_the_money_check_and_by_nothing_else(naive_stack):
    run = live.run_live("naive")
    assert not run.report.passed
    assert run.report.money_off_cents == 6_968  # pay_000003, $69.68, booked twice
    assert run.report.invariant("ledger_balanced").passed
    assert run.report.invariant("payments_match_provider").passed  # our payments table is right; the ledger is not
    assert (run.dashboard.status, run.dashboard.error_rate) == ("GREEN", 0.0)


@pytest.fixture(scope="module")
def naive_confirmer_stack():
    stack.reset()
    stack.up("idempotent", confirmer="by_provider_id")
    yield
    stack.reset()  # do not leave the known-bad baseline running


def test_the_naive_confirmer_leaves_a_charged_payment_pending_and_only_the_fourth_invariant_sees_it(
    naive_confirmer_stack,
):
    run = lost_answer.run_live("by_provider_id")
    assert [i.name for i in run.report.invariants if not i.passed] == ["payments_match_provider"]
    [stuck] = run.report.invariant("payments_match_provider").evidence
    assert (stuck["payment_id"], stuck["problem"], stuck["amount_cents"]) == (
        run.stuck_payment_id,
        "pending but charged",
        6_968,
    )
    assert (run.after_webhooks["status"], run.refund["status"]) == ("pending", 409)  # and it cannot be refunded
    assert (run.dashboard.status, run.dashboard.error_rate) == ("GREEN", 0.0)
