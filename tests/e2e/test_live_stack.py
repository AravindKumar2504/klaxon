"""End to end: the whole Compose stack, driven over HTTP the way a client and an operator would.

These reset the stack (its data is synthetic) and take about two minutes: make e2e.
The tests in this file run in order and share one stack, except the last two, which build their own.
"""

import threading
import time

import httpx
import pytest

from klaxon import traffic
from klaxon.checks.live import PROVIDER_URL, ProviderAPI, check_live
from klaxon.demo import live, lost_answer, under_traffic
from klaxon.patient import stack
from klaxon.patient.provider import PAYMENT_SUCCEEDED
from klaxon.patient.services.kafka_io import CONFIRMER_GROUP, EVENTS_TOPIC, LEDGER_GROUP, consumer_lag

pytestmark = pytest.mark.e2e

API_URL = live.API_URL
KAFKA = "127.0.0.1:9094"
TEMPO, PROMETHEUS, LOKI = "http://127.0.0.1:3200", "http://127.0.0.1:9090", "http://127.0.0.1:3100"


@pytest.fixture(scope="module")
def idempotent_stack():
    stack.reset()
    stack.up("idempotent")
    yield


@pytest.fixture
def api():
    with httpx.Client(base_url=API_URL, timeout=20.0, trust_env=False) as client:
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
    with httpx.Client(base_url=PROVIDER_URL, trust_env=False) as provider:
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
        with httpx.Client(base_url=PROVIDER_URL, trust_env=False) as provider:
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
        with httpx.Client(base_url=API_URL, timeout=20.0, trust_env=False) as client:
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
    with httpx.Client(base_url=API_URL, timeout=20.0, trust_env=False) as client:
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


def test_while_traffic_keeps_flowing_every_money_check_passes(idempotent_stack):
    """Known gap 3, closed: before the cutoff, 19 of 100 checks failed like this on a healthy stack."""
    run = under_traffic.run_live(rate=5.0, checks=8, warmup_seconds=3.0)
    assert run.passed, under_traffic.render(run)
    assert run.traffic["counts"]["payments.succeeded"] > 20  # the checks really ran while payments were made
    assert run.behavior_note == "the provider's behavior was put back as it was found"


def checks_under_traffic(settings: traffic.Settings, checks: int = 3) -> list:
    generator = traffic.TrafficGenerator(settings)
    generator.start()
    try:
        time.sleep(5.0)
        return [check_live() for _ in range(checks)]
    finally:
        generator.close()
        generator.join()


def local_get(url: str, params: dict | None = None) -> httpx.Response:
    """A GET to this Mac's stack, never through a proxy."""
    return httpx.get(url, params=params, timeout=10, trust_env=False)


def eventually(read, timeout: float = 60.0, every: float = 2.0):
    """Telemetry is sent in batches every few seconds: poll until read() returns something."""
    deadline = time.monotonic() + timeout
    while not (value := read()):
        assert time.monotonic() < deadline, "the telemetry never arrived"
        time.sleep(every)
    return value


def test_a_payment_leaves_its_trace_metrics_and_logs_in_the_observability_stack(idempotent_stack, api):
    payment = pay(api, "e2e-telemetry", 1_234).json()
    assert check_live().report.passed  # settled: both consumers have handled the webhook
    charge = payment["provider_payment_id"]
    [event] = [e.id for e in ProviderAPI(PROVIDER_URL).events if e.payment_id == charge]

    def webhook_trace() -> dict | None:
        query = f'{{ span.provider.payment_id = "{charge}" && kind = consumer }}'
        found = local_get(f"{TEMPO}/api/search", {"q": query}).json().get("traces", [])
        if len(found) != 1:
            return None
        spans = local_get(f"{TEMPO}/api/v2/traces/{found[0]['traceID']}").json()["trace"]["resourceSpans"]
        services = {
            a["value"]["stringValue"]
            for rs in spans
            for a in rs["resource"]["attributes"]
            if a["key"] == "service.name"
        }
        # One trace: the receiver's webhook, then both consumers' work. Wait until every part has been exported.
        return found[0]["traceID"] if {"webhook-receiver", "ledger-consumer", "payment-confirmer"} <= services else None

    trace_id = eventually(webhook_trace)

    def counted_every_posting() -> bool:
        # Every booking counted exactly once: the counter (summed over consumer restarts) equals the ledger.
        query = 'sum(klaxon_consumer_events_total{consumer="ledger", outcome="posted"})'
        result = local_get(f"{PROMETHEUS}/api/v1/query", {"query": query}).json()["data"]["result"]
        return bool(result) and float(result[0]["value"][1]) == ledger_transactions()

    assert eventually(counted_every_posting)

    def posted_line() -> list:
        query = f'{{service_name="ledger-consumer"}} | event_id = "{event}"'
        response = httpx.get(
            f"{LOKI}/loki/api/v1/query_range", params={"query": query, "since": "1h"}, timeout=10, trust_env=False
        )
        return response.json()["data"]["result"]

    [stream] = eventually(posted_line)
    assert stream["stream"]["trace_id"] == trace_id  # the log line leads to the same trace
    # payment_id is ours in every service's lines, as in the payments API's; the provider's id has its own name.
    assert (stream["stream"]["payment_id"], stream["stream"]["provider_payment_id"]) == (payment["id"], charge)

    # Every counter series starts with a 0 in Prometheus, where it started counting, so rate() also sees a
    # burst counted before the series' first export (otel-lgtm runs Prometheus with created-timestamp zero ingestion).
    query = "klaxon_webhooks_received_total[1h]"
    series = local_get(f"{PROMETHEUS}/api/v1/query", {"query": query}).json()["data"]["result"]
    assert series and all(s["values"][0][1] == "0" for s in series)


def test_grafana_asks_who_you_are(idempotent_stack):
    """The image lets anyone on this Mac in as Admin by default; compose.yaml turns that off."""
    assert httpx.get("http://127.0.0.1:3000/api/search", timeout=10, trust_env=False).status_code == 401


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
        with httpx.Client(base_url=PROVIDER_URL, trust_env=False) as provider:
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


def test_under_traffic_the_naive_consumer_is_caught_for_counting_twice_and_for_nothing_else(naive_stack):
    """The cutoff must not hide a real fault, nor add false ones next to it."""
    lossy = traffic.Settings(rate=5.0, seed=3, lose_ack_rate=0.2, lost_answer_rate=0.0)
    for result in checks_under_traffic(lossy):
        assert not result.report.passed
        posted = result.report.invariant("posted_exactly_once").evidence
        assert posted and all(item["expected"] == 1 and item["times_posted"] > 1 for item in posted)
        assert result.report.invariant("ledger_balanced").passed
        assert result.report.invariant("payments_match_provider").passed


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


def test_under_traffic_the_naive_confirmer_is_caught_for_charged_payments_left_pending_and_nothing_else(
    naive_confirmer_stack,
):
    lost_answers = traffic.Settings(rate=5.0, seed=3, lose_ack_rate=0.0, lost_answer_rate=0.2)
    results = checks_under_traffic(lost_answers)
    for result in results:
        assert [i.name for i in result.report.invariants if not i.passed] == ["payments_match_provider"]
        stuck = result.report.invariant("payments_match_provider").evidence
        assert stuck and {item["problem"] for item in stuck} == {"pending but charged"}
    assert len(results[-1].report.invariant("payments_match_provider").evidence) > 3  # the lost answers pile up
