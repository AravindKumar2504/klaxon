"""The fake provider service: its records, its webhook sender, and its HTTP API."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from klaxon.patient import signatures
from klaxon.patient.provider import Event
from klaxon.patient.services.provider_app import create_app
from klaxon.patient.services.provider_delivery import (
    MAX_AGE_SECONDS,
    Behavior,
    WebhookSender,
    backoff_seconds,
)
from klaxon.patient.services.provider_store import ProviderError, ProviderStore

SECRET = b"test-secret-not-real"


class Clock:
    def __init__(self, now: float = 1_791_504_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock) -> ProviderStore:
    return ProviderStore(":memory:", clock=clock)


class Receiver:
    """Our endpoint as the provider sees it: checks the signature, answers with scripted status codes."""

    def __init__(self, clock: Clock, statuses: list[int] | None = None) -> None:
        self.clock = clock
        self.statuses = list(statuses or [])
        self.received: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        signatures.verify(request.headers[signatures.HEADER], request.content, SECRET, int(self.clock()))
        self.received.append(json.loads(request.content))
        return httpx.Response(self.statuses.pop(0) if self.statuses else 200)


def sender_for(store, clock, handler, behavior=None) -> WebhookSender:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return WebhookSender(store, "http://receiver/webhooks/provider", SECRET, client, behavior, clock=clock)


# Records -------------------------------------------------------------------------


def test_a_payment_writes_the_payment_its_event_and_a_pending_delivery_together(store, clock):
    payment, replayed = store.create_payment("cus_001", 2_500)
    assert (payment["id"], payment["status"], replayed) == ("pay_000001", "succeeded", False)
    events, has_more = store.list_events()
    assert [(e["id"], e["type"], e["data"]["payment_id"]) for e in events] == [
        ("evt_000001", "payment.succeeded", "pay_000001")
    ]
    assert has_more is False
    assert [d.event_id for d in store.due_deliveries(clock())] == ["evt_000001"]


def test_event_bodies_are_the_first_pieces_payload_plus_the_payments_metadata(store, clock):
    store.create_payment("cus_001", 2_500, metadata={"klaxon_payment_id": "pmt_000001"})
    store.create_refund("pay_000001", 500)
    paid, refunded = (d.body for d in store.due_deliveries(clock()))
    expected = Event("evt_000001", "payment.succeeded", "pay_000001", 2_500, int(clock())).payload()
    expected["data"]["metadata"] = {"klaxon_payment_id": "pmt_000001"}  # how our side finds its own payment
    assert paid == json.dumps(expected, separators=(",", ":"), sort_keys=True).encode()
    refund = Event("evt_000002", "refund.succeeded", "pay_000001", 500, int(clock())).payload()
    assert refunded == json.dumps(refund, separators=(",", ":"), sort_keys=True).encode()  # unchanged


def test_a_repeated_idempotency_key_returns_the_first_payment_and_creates_nothing(store):
    first, _ = store.create_payment("cus_001", 2_500, idempotency_key="k1")
    again, replayed = store.create_payment("cus_001", 2_500, idempotency_key="k1")
    assert (again, replayed) == (first, True)
    assert store.balance_cents() == 2_500
    assert len(store.list_events()[0]) == 1


def test_a_key_reused_for_a_different_request_is_refused(store):
    store.create_payment("cus_001", 2_500, idempotency_key="k1")
    with pytest.raises(ProviderError) as refused:
        store.create_payment("cus_001", 9_900, idempotency_key="k1")
    assert refused.value.code == "idempotency_key_reused"
    with pytest.raises(ProviderError):
        store.create_refund("pay_000001", 100, idempotency_key="k1")  # same key, another endpoint
    assert store.balance_cents() == 2_500


def test_a_request_that_fails_validation_saves_nothing_under_its_key(store):
    with pytest.raises(ProviderError):
        store.create_payment("cus_001", 0, idempotency_key="k1")
    payment, replayed = store.create_payment("cus_001", 2_500, idempotency_key="k1")
    assert (payment["id"], replayed) == ("pay_000001", False)


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "100"])
def test_amounts_must_be_whole_positive_cents(store, amount):
    with pytest.raises(ProviderError):
        store.create_payment("cus_001", amount)
    assert store.list_events()[0] == []


def test_only_usd(store):
    with pytest.raises(ProviderError) as refused:
        store.create_payment("cus_001", 100, currency="eur")
    assert refused.value.code == "unsupported_currency"


def test_refunds_cannot_exceed_what_was_captured(store):
    store.create_payment("cus_001", 2_500)
    store.create_refund("pay_000001", 1_000)
    store.create_refund("pay_000001", 1_500)
    with pytest.raises(ProviderError) as refused:
        store.create_refund("pay_000001", 1)
    assert refused.value.code == "refund_exceeds_payment"
    with pytest.raises(ProviderError) as missing:
        store.create_refund("pay_999999", 1)
    assert missing.value.status == 404
    assert store.balance_cents() == 0
    assert store.get_payment("pay_000001")["amount_refunded_cents"] == 2_500


def test_events_page_oldest_first(store):
    for n in range(5):
        store.create_payment(f"cus_{n}", 100 + n)
    page1, more1 = store.list_events(limit=2)
    page2, more2 = store.list_events(limit=2, starting_after=page1[-1]["id"])
    page3, more3 = store.list_events(limit=2, starting_after=page2[-1]["id"])
    assert [e["id"] for e in page1 + page2 + page3] == [f"evt_00000{n}" for n in range(1, 6)]
    assert (more1, more2, more3) == (True, True, False)
    with pytest.raises(ProviderError):
        store.list_events(starting_after="evt_nope")


def test_payments_page_oldest_first_with_their_metadata(store):
    for n in range(1, 4):
        store.create_payment(f"cus_{n}", 100 * n, metadata={"klaxon_payment_id": f"pmt_00000{n}"})
    store.create_refund("pay_000002", 50)
    page1, more1 = store.list_payments(limit=2)
    page2, more2 = store.list_payments(limit=2, starting_after=page1[-1]["id"])
    assert [(p["id"], p["metadata"]["klaxon_payment_id"]) for p in page1 + page2] == [
        ("pay_000001", "pmt_000001"),
        ("pay_000002", "pmt_000002"),
        ("pay_000003", "pmt_000003"),
    ]
    assert (more1, more2) == (True, False)
    assert page1[1]["amount_refunded_cents"] == 50
    with pytest.raises(ProviderError):
        store.list_payments(starting_after="pay_nope")


def test_records_survive_a_restart(tmp_path, clock):
    path = str(tmp_path / "provider.sqlite3")
    ProviderStore(path, clock=clock).create_payment("cus_001", 2_500)
    reopened = ProviderStore(path, clock=clock)
    assert reopened.balance_cents() == 2_500
    assert [d.event_id for d in reopened.due_deliveries(clock())] == ["evt_000001"]


# Webhook sender ------------------------------------------------------------------


def test_deliveries_are_signed_with_the_shared_secret_and_marked_delivered(store, clock):
    store.create_payment("cus_001", 2_500)
    receiver = Receiver(clock)
    assert sender_for(store, clock, receiver).deliver_due() == 1
    assert [e["id"] for e in receiver.received] == ["evt_000001"]
    assert store.delivery_stats()["pending"] == 0
    assert store.due_deliveries(clock.now + 3600) == []


def test_a_failed_delivery_is_retried_after_a_backoff_until_it_is_acknowledged(store, clock):
    store.create_payment("cus_001", 2_500)
    receiver = Receiver(clock, statuses=[500, 503, 200])
    sender = sender_for(store, clock, receiver)
    assert sender.deliver_due() == 1
    assert sender.deliver_due() == 0  # not due yet: backing off
    clock.now += backoff_seconds(1)
    sender.deliver_due()
    clock.now += backoff_seconds(2)
    sender.deliver_due()
    stats = store.delivery_stats()
    assert (stats["attempts"], stats["answered_5xx"], stats["delivered"], stats["pending"]) == (3, 2, 1, 0)
    assert [a["status"] for a in store.delivery_attempts()] == [500, 503, 200]


def test_no_answer_at_all_is_retried_too(store, clock):
    store.create_payment("cus_001", 2_500)

    def refuse(request):
        raise httpx.ConnectError("connection refused")

    sender_for(store, clock, refuse).deliver_due()
    attempt = store.delivery_attempts()[0]
    assert attempt["status"] is None and "ConnectError" in attempt["error"]
    assert store.delivery_stats()["pending"] == 1


def test_a_lost_acknowledgement_makes_the_provider_send_the_event_again(store, clock):
    for n in range(3):
        store.create_payment(f"cus_{n}", 1_000 + n)
    receiver = Receiver(clock)
    sender = sender_for(store, clock, receiver, Behavior(lose_ack_event_ids=frozenset({"evt_000002"})))
    sender.deliver_due()
    clock.now += backoff_seconds(1)
    sender.deliver_due()
    assert [e["id"] for e in receiver.received] == ["evt_000001", "evt_000002", "evt_000003", "evt_000002"]
    stats = store.delivery_stats()
    assert (stats["duplicate_deliveries"], stats["acks_lost_on_purpose"], stats["pending"]) == (1, 1, 0)


def test_a_seeded_lose_rate_loses_the_same_acknowledgements_every_time():
    behavior = Behavior(lose_ack_rate=0.5, seed=7)
    draws = [behavior.ack_lost(f"evt_{n:06d}", 1) for n in range(1, 41)]
    assert draws == [Behavior(lose_ack_rate=0.5, seed=7).ack_lost(f"evt_{n:06d}", 1) for n in range(1, 41)]
    assert 0 < sum(draws) < 40
    assert not any(Behavior(lose_ack_rate=0.0).ack_lost(f"evt_{n}", 1) for n in range(40))
    with pytest.raises(ValueError):
        Behavior(lose_ack_rate=1.5)


def test_after_three_days_of_failures_the_provider_gives_up(store, clock):
    store.create_payment("cus_001", 2_500)
    clock.now += MAX_AGE_SECONDS + 1
    sender_for(store, clock, Receiver(clock, statuses=[500])).deliver_due()
    stats = store.delivery_stats()
    assert (stats["abandoned"], stats["pending"]) == (1, 0)


def test_a_seeded_failure_after_the_charge_picks_the_same_keys_every_time():
    behavior = Behavior(fail_after_charge_rate=0.5, seed=7)
    draws = [behavior.fails_after_charge(f"pmt_{n:06d}") for n in range(1, 41)]
    assert draws == [
        Behavior(fail_after_charge_rate=0.5, seed=7).fails_after_charge(f"pmt_{n:06d}") for n in range(1, 41)
    ]
    assert 0 < sum(draws) < 40
    assert Behavior(fail_after_charge_keys=frozenset({"pmt_000003"})).fails_after_charge("pmt_000003")
    assert not any(Behavior().fails_after_charge(f"pmt_{n:06d}") for n in range(40))
    with pytest.raises(ValueError):
        Behavior(fail_after_charge_rate=-0.1)


def test_backoff_doubles_and_is_capped_at_a_minute():
    assert [backoff_seconds(n) for n in range(1, 10)] == [0.5, 1, 2, 4, 8, 16, 32, 60, 60]


# HTTP API ------------------------------------------------------------------------


@pytest.fixture
def api(store) -> TestClient:
    return TestClient(create_app(store))


def test_http_create_payment_with_a_repeated_key(api):
    first = api.post(
        "/v1/payments", json={"customer": "cus_001", "amount_cents": 2_500}, headers={"Idempotency-Key": "k"}
    )
    again = api.post(
        "/v1/payments", json={"customer": "cus_001", "amount_cents": 2_500}, headers={"Idempotency-Key": "k"}
    )
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert "idempotent-replayed" not in first.headers and again.headers["idempotent-replayed"] == "true"
    assert api.get("/v1/balance").json()["available_cents"] == 2_500


@pytest.mark.parametrize(
    "payload",
    [
        {"customer": "cus_001", "amount_cents": 25.0},
        {"customer": "cus_001", "amount_cents": "2500"},
        {"customer": "cus_001", "amount_cents": True},
        {"customer": "cus_001", "amount_cents": 0},
        {"customer": "cus_001", "amount_cents": 100, "currency": "eur"},
        {"customer": "", "amount_cents": 100},
        {"customer": "cus_001", "amount_cents": 100, "surprise": 1},
    ],
)
def test_http_refuses_bad_payment_requests(api, payload):
    assert api.post("/v1/payments", json=payload).status_code == 422
    assert api.get("/v1/events").json()["data"] == []


def test_http_errors_have_a_code_and_a_message(api):
    api.post("/v1/payments", json={"customer": "cus_001", "amount_cents": 2_500})
    too_much = api.post("/v1/refunds", json={"payment_id": "pay_000001", "amount_cents": 2_501})
    assert too_much.status_code == 400
    assert too_much.json()["error"]["code"] == "refund_exceeds_payment"
    assert api.get("/v1/payments/pay_404").status_code == 404


def test_http_events_and_deliveries(api):
    api.post("/v1/payments", json={"customer": "cus_001", "amount_cents": 2_500})
    api.post("/v1/refunds", json={"payment_id": "pay_000001", "amount_cents": 500})
    events = api.get("/v1/events", params={"limit": 1}).json()
    assert [e["id"] for e in events["data"]] == ["evt_000001"] and events["has_more"] is True
    rest = api.get("/v1/events", params={"starting_after": "evt_000001"}).json()
    assert [(e["id"], e["type"]) for e in rest["data"]] == [("evt_000002", "refund.succeeded")]
    assert api.get("/admin/deliveries").json()["stats"]["pending"] == 2
    assert api.put("/admin/behavior", json={"lose_ack_rate": 0.1}).status_code == 409  # no sender in this test
    assert api.get("/healthz").json() == {"status": "ok"}


def test_http_lists_payments(api):
    api.post(
        "/v1/payments", json={"customer": "cus_001", "amount_cents": 2_500, "metadata": {"klaxon_payment_id": "pmt_1"}}
    )
    api.post("/v1/payments", json={"customer": "cus_002", "amount_cents": 900})
    first = api.get("/v1/payments", params={"limit": 1}).json()
    assert [p["id"] for p in first["data"]] == ["pay_000001"] and first["has_more"] is True
    assert first["data"][0]["metadata"] == {"klaxon_payment_id": "pmt_1"}
    rest = api.get("/v1/payments", params={"starting_after": "pay_000001"}).json()
    assert [(p["id"], p["metadata"]) for p in rest["data"]] == [("pay_000002", {})]


def test_charged_then_500_saves_the_charge_and_answers_every_retry_the_same(store, clock):
    """Like Stripe replaying a key's first answer: our side never hears that the charge went through."""
    sender = sender_for(store, clock, Receiver(clock))
    sender.behavior = Behavior(fail_after_charge_keys=frozenset({"pmt_000003"}))
    api = TestClient(create_app(store, sender))
    payment = {"customer": "cus_003", "amount_cents": 6_968, "metadata": {"klaxon_payment_id": "pmt_000003"}}
    answers = [api.post("/v1/payments", json=payment, headers={"Idempotency-Key": "pmt_000003"}) for _ in range(3)]
    assert [a.status_code for a in answers] == [500, 500, 500]
    assert answers[0].json()["error"]["code"] == "internal_error"
    assert [p["id"] for p in store.list_payments()[0]] == ["pay_000001"]  # charged once, not three times
    assert store.balance_cents() == 6_968
    assert [d.event_id for d in store.due_deliveries(clock())] == ["evt_000001"]  # the webhook will tell the truth
    other = api.post(
        "/v1/payments", json={"customer": "cus_004", "amount_cents": 100}, headers={"Idempotency-Key": "k4"}
    )
    assert other.status_code == 200  # only the chosen key fails


def test_http_behavior_sets_and_shows_every_control(store, clock):
    api = TestClient(create_app(store, sender_for(store, clock, Receiver(clock))))
    chosen = {"lose_ack_rate": 0.2, "seed": 3, "fail_after_charge_rate": 1.0, "fail_after_charge_keys": ["pmt_1"]}
    assert api.put("/admin/behavior", json=chosen).status_code == 200
    shown = api.get("/admin/behavior").json()
    assert shown == chosen | {"lose_ack_event_ids": []}
    assert api.put("/admin/behavior", json={"fail_after_charge_rate": 2}).status_code == 422
