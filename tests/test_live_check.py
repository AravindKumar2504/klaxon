"""klaxon.checks.live and the live demo's scenario, without Docker: fake provider pages, fake settling state."""

import httpx

from klaxon.checks.live import ProviderAPI, settling_state, wait_until_settled
from klaxon.demo.live import scenario_from_first_piece
from klaxon.patient.provider import Event


def provider_pages(total: int, page_size: int):
    events = [
        {
            "id": f"evt_{n:06d}",
            "type": "payment.succeeded",
            "created": 1_791_504_000 + n,
            "data": {"payment_id": f"pay_{n:06d}", "amount_cents": 100 * n, "currency": "usd"},
        }
        for n in range(1, total + 1)
    ]
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/balance":
            return httpx.Response(200, json={"available_cents": sum(e["data"]["amount_cents"] for e in events)})
        asked.append(dict(request.url.params))
        after = request.url.params.get("starting_after")
        start = 0 if after is None else next(i for i, e in enumerate(events) if e["id"] == after) + 1
        page = events[start : start + page_size]
        return httpx.Response(200, json={"data": page, "has_more": start + page_size < len(events)})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://provider"), asked


def test_the_provider_snapshot_pages_through_every_event():
    http, asked = provider_pages(total=5, page_size=2)
    snapshot = ProviderAPI(http=http, page_size=2)
    assert [e.id for e in snapshot.events] == [f"evt_{n:06d}" for n in range(1, 6)]
    assert snapshot.events[2] == Event("evt_000003", "payment.succeeded", "pay_000003", 300, 1_791_504_003)
    assert snapshot.balance_cents() == 1_500
    assert [a.get("starting_after") for a in asked] == [None, "evt_000002", "evt_000004"]


def test_an_empty_provider_is_an_empty_snapshot():
    http, _ = provider_pages(total=0, page_size=2)
    snapshot = ProviderAPI(http=http)
    assert (snapshot.events, snapshot.balance_cents()) == ([], 0)


class Timeline:
    """A scripted settling state and a clock that advances only when the waiter sleeps."""

    def __init__(self, states):
        self.states = list(states)
        self.now = 0.0

    def state(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def sleep(self, seconds):
        self.now += seconds


def wait(timeline, timeout=10.0):
    return wait_until_settled(timeout, 0.5, timeline.state, lambda: timeline.now, timeline.sleep)


def test_settled_means_nothing_pending_and_no_lag_twice_in_a_row():
    timeline = Timeline([(2, {0: 3}), (0, {0: 1}), (0, {0: 0}), (0, {0: 0})])
    result = wait(timeline)
    assert result.settled and result.waited_seconds == 1.5


def test_new_activity_resets_the_quiet_count():
    timeline = Timeline([(0, {0: 0}), (1, {0: 0}), (0, {0: 0}), (0, {0: 0})])
    assert wait(timeline).waited_seconds == 1.5


def test_a_system_that_never_settles_times_out_with_what_it_saw():
    timeline = Timeline([(1, {0: 2, 1: 3})])
    result = wait(timeline, timeout=2.0)
    assert not result.settled
    assert (result.pending_deliveries, result.ledger_lag, result.lag_by_partition) == (1, 5, {0: 2, 1: 3})


def test_the_live_demo_replays_the_first_pieces_scenario():
    scenario = scenario_from_first_piece(seed=7)
    assert scenario.payments == [
        ("cus_001", 5_805),
        ("cus_002", 2_971),
        ("cus_003", 6_968),
        ("cus_004", 11_164),
        ("cus_005", 1_291),
    ]
    assert (scenario.refund_of, scenario.refund_cents) == (1, 2_902)
    assert (scenario.lost_ack_position, scenario.lost_ack_event_id) == (3, "evt_000003")


def test_the_provider_is_read_before_kafka():
    """Pending first: once nothing is pending, every event is already in Kafka, so the lag read after covers it."""
    order = []
    state = settling_state(lambda: order.append("provider") or 0, lambda: order.append("kafka") or {0: 0})
    assert order == ["provider", "kafka"]
    assert state == (0, {0: 0})
