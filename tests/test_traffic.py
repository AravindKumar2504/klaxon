"""The traffic generator, offline: its seeded plan, what a customer sends, and how it retries and gives up.

The stack is played by httpx mock transports, the clock moves only when the generator waits, and every
task runs at once in the test's thread, so each run is exact.
"""

import json
from statistics import mean

import httpx
import pytest

from klaxon import traffic
from klaxon.traffic import (
    PlannedPayment,
    Settings,
    TrafficGenerator,
    payment_outcome,
    plan,
    refund_outcome,
    retryable,
)

DEFAULT_BEHAVIOR = {
    "lose_ack_rate": 0.0,
    "seed": 0,
    "lose_ack_event_ids": [],
    "fail_after_charge_rate": 0.0,
    "fail_after_charge_keys": [],
    "fail_after_refund_rate": 0.0,
    "fail_after_refund_keys": [],
}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.now += seconds


class Stack:
    """The payments API and the fake provider's admin API, with scripted answers."""

    def __init__(self, clock: Clock, answers=None) -> None:
        self.clock = clock
        self.answers = answers or (lambda request, n: httpx.Response(201, json=self.payment(request)))
        self.sent: list[tuple[float, str, str, str | None, dict | None]] = []
        self.behavior = dict(DEFAULT_BEHAVIOR)
        self.behavior_puts: list[dict] = []
        self._ids: dict[str, str] = {}

    def payment(self, request: httpx.Request, status: str = "succeeded") -> dict:
        key = request.headers["Idempotency-Key"]
        body = json.loads(request.content)
        payment_id = self._ids.setdefault(key, f"pmt_{len(self._ids) + 1:06d}")
        return {"id": payment_id, "status": status, "amount_cents": body.get("amount_cents")}

    def api_handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        key = request.headers.get("Idempotency-Key")
        n = sum(1 for s in self.sent if (s[1], s[2], s[3]) == (request.method, request.url.path, key)) + 1
        self.sent.append((self.clock(), request.method, request.url.path, key, body))
        return self.answers(request, n)

    def provider_handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.behavior = DEFAULT_BEHAVIOR | json.loads(request.content)
            self.behavior_puts.append(json.loads(request.content))
        return httpx.Response(200, json=self.behavior)

    def generator(self, settings: Settings, submit=None, payments=None) -> TrafficGenerator:
        return TrafficGenerator(
            settings,
            api=httpx.Client(transport=httpx.MockTransport(self.api_handler), base_url="http://127.0.0.1:8080"),
            provider=httpx.Client(
                transport=httpx.MockTransport(self.provider_handler), base_url="http://127.0.0.1:8081"
            ),
            clock=self.clock,
            submit=submit or (lambda task: task()),
            wait=self.clock.wait,
            payments=payments,
        )

    def posts(self, path_start: str = "/payments") -> list[tuple]:
        return [s for s in self.sent if s[1] == "POST" and s[2].startswith(path_start)]


def one_payment(refund_after: float | None = None, double_click: bool = False, in_full: bool = True) -> list:
    """A plan with a single customer, at 0.2 s: timings in a test are then exact."""
    return [PlannedPayment(1, 0.2, "cus_001", 4_001, double_click, refund_after, in_full)]


def quiet(**overrides) -> Settings:
    """No noise and no extras unless a test asks for them."""
    return Settings(
        **{
            "rate": 2.0,
            "seconds": 10.0,
            "lose_ack_rate": 0.0,
            "lost_answer_rate": 0.0,
            "double_click_share": 0.0,
            "refund_share": 0.0,
            "run_id": "r1",
        }
        | overrides
    )


# The plan -------------------------------------------------------------------------


def first(n: int, **kwargs) -> list[PlannedPayment]:
    payments = plan(**kwargs)
    return [next(payments) for _ in range(n)]


def test_the_same_seed_and_rate_give_the_same_customers_amounts_and_choices():
    assert first(50, seed=7, rate=2.0) == first(50, seed=7, rate=2.0)
    assert first(50, seed=7, rate=2.0) != first(50, seed=8, rate=2.0)


def test_one_choice_never_shifts_the_rest_of_the_plan():
    """Turning double clicks on changes which payments are clicked twice, not their customers or amounts."""
    plain = first(200, seed=7, rate=2.0, double_click_share=0.0, refund_share=0.0)
    busy = first(200, seed=7, rate=2.0, double_click_share=0.5, refund_share=0.5)
    assert [(p.at, p.customer, p.amount_cents) for p in plain] == [(p.at, p.customer, p.amount_cents) for p in busy]


def test_the_plan_looks_like_customers():
    payments = first(10_000, seed=3, rate=4.0)
    gaps = [b.at - a.at for a, b in zip(payments, payments[1:], strict=False)]
    assert mean(gaps) == pytest.approx(0.25, rel=0.05)  # 4 per second on average
    assert all(traffic.MIN_CENTS <= p.amount_cents <= traffic.MAX_CENTS for p in payments)
    amounts = sorted(p.amount_cents for p in payments)
    assert 3_500 < amounts[len(amounts) // 2] < 4_500  # the median is about $40
    assert 1_000 < amounts[len(amounts) // 10] and amounts[len(amounts) * 9 // 10] < 15_000  # mostly $10 to $150
    assert sum(p.double_click for p in payments) / len(payments) == pytest.approx(0.02, abs=0.005)
    refunded = [p for p in payments if p.refund_after is not None]
    assert len(refunded) / len(payments) == pytest.approx(0.10, abs=0.01)
    assert all(2.0 <= p.refund_after <= 20.0 for p in refunded)
    assert all(p.customer.startswith("cus_") and len(p.customer) == 7 for p in payments)


def test_a_refund_is_the_whole_amount_or_half():
    whole = PlannedPayment(1, 0.0, "cus_001", 4_001, False, 3.0, True)
    half = PlannedPayment(1, 0.0, "cus_001", 4_001, False, 3.0, False)
    assert (whole.refund_cents, half.refund_cents) == (4_001, 2_000)


# Settings -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://example.com:8080", "http://10.0.0.5:8080", "https://payments.example.org", "http://127.0.0.2"]
)
def test_traffic_only_ever_goes_to_this_mac(url):
    with pytest.raises(ValueError, match="only this Mac"):
        Settings(api_url=url)
    with pytest.raises(ValueError, match="only this Mac"):
        Settings(provider_url=url)


@pytest.mark.parametrize(
    "bad",
    [
        {"rate": 0},
        {"rate": 101},
        {"seconds": 0},
        {"lose_ack_rate": 1.5},
        {"refund_share": -0.1},
        {"max_in_flight": 0},
    ],
)
def test_settings_out_of_range_are_refused(bad):
    with pytest.raises(ValueError):
        Settings(**bad)


# What a customer sends ------------------------------------------------------------


def test_payments_follow_the_plan_with_one_key_each_and_stop_when_the_time_is_up():
    clock = Clock()
    stack = Stack(clock)
    stats = stack.generator(quiet(seconds=10.0)).run()
    planned = [p for p in first(100, seed=7, rate=2.0, double_click_share=0.0, refund_share=0.0) if p.at < 10.0]
    posts = stack.posts()
    assert [(key, body) for _, _, _, key, body in posts] == [
        (f"traffic-r1-pay-{p.n}", {"customer": p.customer, "amount_cents": p.amount_cents}) for p in planned
    ]
    assert all(at >= p.at for (at, *_), p in zip(posts, planned, strict=True))  # none sent early
    assert stats["payments.succeeded"] == stats["payments.arrived"] == len(planned)
    assert 10.0 <= clock.now < 10.5


def unknown_then(status_later: str):
    """The provider's answer is lost every time (503); asked later, the payment is status_later."""

    def answers(request, n):
        if request.method == "GET":
            return httpx.Response(200, json={"id": "pmt_000001", "status": status_later})
        error = {"code": "provider_unavailable", "message": "the outcome is unknown"}
        return httpx.Response(503, json={"error": error, "payment": {"id": "pmt_000001", "status": "pending"}})

    return answers


def test_an_unknown_outcome_is_retried_with_the_same_key_then_given_up():
    clock = Clock()
    stack = Stack(clock, unknown_then("succeeded"))
    stats = stack.generator(quiet(seconds=4.0), payments=one_payment()).run()
    attempts = [(at, key) for at, method, _, key, _ in stack.sent if method == "POST"]
    # The same key every time, 0.5, 1 and 2 seconds apart: a retry can never become a second payment.
    assert [key for _, key in attempts] == ["traffic-r1-pay-1"] * 4
    assert [b - a for (a, _), (b, _) in zip(attempts, attempts[1:], strict=False)] == pytest.approx([0.5, 1.0, 2.0])
    assert (stats["retries"], stats["retries.after_503"], stats["payments.unknown"]) == (3, 3, 1)
    # The run ended before the status check was due: it is counted, not sent.
    assert stats["not_sent.status_checks"] == 1 and not [s for s in stack.sent if s[1] == "GET"]


def test_a_payment_whose_outcome_is_unknown_is_asked_about_5_seconds_after_giving_up():
    clock = Clock()
    stack = Stack(clock, unknown_then("succeeded"))
    stats = stack.generator(quiet(seconds=12.0), payments=one_payment()).run()
    gave_up_at = stack.posts()[-1][0]
    [(asked_at, _, path, _, _)] = [s for s in stack.sent if s[1] == "GET"]
    assert path == "/payments/pmt_000001" and asked_at - gave_up_at == pytest.approx(5.0, abs=0.3)
    assert stats["unknown_payments.confirmed_later"] == 1  # its webhook settled it, through the confirmer


def test_a_payment_still_pending_is_asked_about_once_more_a_minute_after_giving_up():
    clock = Clock()
    stack = Stack(clock, unknown_then("pending"))  # nothing ever confirms it
    stats = stack.generator(quiet(seconds=80.0), payments=one_payment()).run()
    gave_up_at = stack.posts()[-1][0]
    gets = [at - gave_up_at for at, method, *_ in stack.sent if method == "GET"]
    assert gets == pytest.approx([5.0, 60.0], abs=0.3)
    assert stats["unknown_payments.still_pending"] == 1


def test_a_request_still_running_is_retried_after_its_retry_after():
    clock = Clock()

    def answers(request, n):
        if n == 1:
            error = {"code": "request_in_progress", "message": "still running"}
            return httpx.Response(409, json={"error": error}, headers={"Retry-After": "1"})
        return httpx.Response(201, json=stack.payment(request))

    stack = Stack(clock, answers)
    stats = stack.generator(quiet(seconds=3.0), payments=one_payment()).run()
    (first_at, *_), (second_at, *_) = stack.posts()
    assert second_at - first_at == pytest.approx(1.0)  # Retry-After (1 s) beats the first backoff (0.5 s)
    assert (stats["retries.after_409"], stats["payments.succeeded"]) == (1, 1)


@pytest.mark.parametrize(
    ("status", "code", "outcome"),
    [
        (402, "payment_failed", "failed"),
        (422, "idempotency_key_reused", "error"),
        (400, "idempotency_key_required", "error"),
    ],
)
def test_final_answers_are_not_retried(status, code, outcome):
    clock = Clock()
    stack = Stack(clock, lambda request, n: httpx.Response(status, json={"error": {"code": code}}))
    stats = stack.generator(quiet(seconds=5.0), payments=one_payment()).run()
    assert len(stack.posts()) == 1 and stats["retries"] == 0
    assert stats[f"payments.{outcome}"] == 1


def test_no_answer_at_all_is_retried_too():
    clock = Clock()

    def answers(request, n):
        if n < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(201, json=stack.payment(request))

    stack = Stack(clock, answers)
    stats = stack.generator(quiet(seconds=5.0), payments=one_payment()).run()
    assert (stats["retries.after_no_answer"], stats["payments.succeeded"]) == (2, 1)


@pytest.mark.parametrize(("in_full", "cents"), [(True, 4_001), (False, 2_000)])
def test_a_refund_follows_its_payment_with_its_own_key_and_the_planned_amount(in_full, cents):
    clock = Clock()
    stack = Stack(clock)
    stats = stack.generator(quiet(seconds=10.0), payments=one_payment(refund_after=7.5, in_full=in_full)).run()
    [(paid_at, *_)] = stack.posts("/payments")[:1]
    [(refunded_at, _, _, key, body)] = stack.posts("/payments/pmt_000001/refunds")
    assert (key, body) == ("traffic-r1-refund-1", {"amount_cents": cents})
    assert refunded_at - paid_at == pytest.approx(7.5, abs=0.3)
    assert stats["refunds.succeeded"] == 1


def test_a_refund_the_run_ends_before_is_counted_and_not_sent():
    clock = Clock()
    stack = Stack(clock)
    stats = stack.generator(quiet(seconds=5.0), payments=one_payment(refund_after=7.5)).run()
    assert stack.posts("/payments/pmt_000001/refunds") == [] and stats["not_sent.refunds"] == 1


def test_a_refund_the_api_refuses_is_counted_as_refused_and_not_retried():
    clock = Clock()

    def answers(request, n):
        if request.url.path.endswith("/refunds"):
            return httpx.Response(409, json={"error": {"code": "payment_not_settled"}})
        return httpx.Response(201, json=stack.payment(request))

    stack = Stack(clock, answers)
    stats = stack.generator(quiet(seconds=10.0), payments=one_payment(refund_after=3.0)).run()
    assert len(stack.posts("/payments/pmt_000001/refunds")) == 1
    assert stats["refunds.refused"] == 1 and stats["retries"] == 0


def refund_unknown_then(status_later: str):
    """The payment succeeds; its refund's answer is lost every time (503); asked later, the refund is status_later."""

    def answers(request, n):
        if request.method == "GET":
            refunds = [{"id": "rfd_000009", "status": "succeeded"}, {"id": "rfd_000001", "status": status_later}]
            return httpx.Response(200, json={"id": "pmt_000001", "status": "succeeded", "refunds": refunds})
        if request.url.path.endswith("/refunds"):
            error = {"code": "provider_unavailable", "message": "the outcome is unknown"}
            return httpx.Response(503, json={"error": error, "refund": {"id": "rfd_000001", "status": "pending"}})
        return httpx.Response(201, json={"id": "pmt_000001", "status": "succeeded"})

    return answers


def test_a_refund_whose_outcome_is_unknown_is_asked_about_5_seconds_after_giving_up():
    clock = Clock()
    stack = Stack(clock, refund_unknown_then("succeeded"))
    stats = stack.generator(quiet(seconds=20.0), payments=one_payment(refund_after=3.0)).run()
    refund_posts = stack.posts("/payments/pmt_000001/refunds")
    assert [key for *_, key, _ in refund_posts] == ["traffic-r1-refund-1"] * 4  # retried with its own key
    [(asked_at, _, path, _, _)] = [s for s in stack.sent if s[1] == "GET"]
    assert path == "/payments/pmt_000001" and asked_at - refund_posts[-1][0] == pytest.approx(5.0, abs=0.3)
    assert (stats["refunds.unknown"], stats["unknown_refunds.confirmed_later"]) == (1, 1)
    assert stats["unknown_payments.confirmed_later"] == 0  # the payment's own status does not count


def test_a_refund_still_pending_is_asked_about_once_more_a_minute_after_giving_up():
    clock = Clock()
    stack = Stack(clock, refund_unknown_then("pending"))  # nothing ever confirms it
    stats = stack.generator(quiet(seconds=80.0), payments=one_payment(refund_after=3.0)).run()
    gave_up_at = stack.posts("/payments/pmt_000001/refunds")[-1][0]
    gets = [at - gave_up_at for at, method, *_ in stack.sent if method == "GET"]
    assert gets == pytest.approx([5.0, 60.0], abs=0.3)
    assert stats["unknown_refunds.still_pending"] == 1


def test_a_double_click_sends_the_same_request_twice_with_one_key():
    clock = Clock()
    stack = Stack(clock)
    stats = stack.generator(quiet(seconds=1.0), payments=one_payment(double_click=True)).run()
    [first_click, second_click] = stack.posts()
    assert first_click[3:] == second_click[3:]  # same key, same body
    assert stats["double_clicks.sent"] == 1
    # Here the copy ran first (tasks run one at a time in this test), so it got the fresh answer.
    assert stats["double_clicks.ran_first"] == 1 and stats["payments.succeeded"] == 1


def test_arrivals_past_the_in_flight_limit_are_shed_not_queued():
    clock = Clock()
    stack = Stack(clock)
    parked = []  # tasks that never finish: as if every request hung
    stats = stack.generator(quiet(seconds=5.0, max_in_flight=3), submit=parked.append).run()
    assert len(parked) == 3
    assert stats["shed"] == stats["payments.arrived"] - 3 > 0


def test_a_bug_in_the_generator_is_counted_and_the_traffic_goes_on():
    clock = Clock()
    stack = Stack(clock, lambda request, n: httpx.Response(201, json=None))  # 201 with no payment in it
    stats = stack.generator(quiet(seconds=30.0, rate=0.2, refund_share=1.0)).run()
    assert stats["generator_errors"] == stats["payments.arrived"] > 1  # every one failed, and it kept going
    assert stats.to_dict()["unexpected"]


# The provider's noise ---------------------------------------------------------------


def test_noise_is_switched_on_for_the_run_and_put_back_as_it_was_found():
    clock = Clock()
    stack = Stack(clock)
    stack.behavior = DEFAULT_BEHAVIOR | {"lose_ack_rate": 0.3, "seed": 11}  # someone's earlier setting
    generator = stack.generator(quiet(seconds=1.0, lose_ack_rate=0.05, lost_answer_rate=0.02, seed=9))
    generator.run()
    assert stack.behavior_puts[0] == DEFAULT_BEHAVIOR | {
        "lose_ack_rate": 0.05,
        "fail_after_charge_rate": 0.02,
        "fail_after_refund_rate": 0.02,  # refunds lose their answers as often as charges
        "seed": 9,
    }
    assert stack.behavior == DEFAULT_BEHAVIOR | {"lose_ack_rate": 0.3, "seed": 11}
    assert generator.behavior_note == "the provider's behavior was put back as it was found"


def test_a_setting_someone_else_made_meanwhile_survives_the_noise_being_put_back():
    clock = Clock()
    stack = Stack(clock)
    generator = stack.generator(quiet(seconds=1.0, lose_ack_rate=0.05))
    original_wait = clock.wait

    def wait(seconds):
        stack.behavior = stack.behavior | {"fail_after_charge_keys": ["pmt_000003"]}  # a fault scenario's setting
        original_wait(seconds)

    generator._wait = wait
    generator.run()
    assert stack.behavior["fail_after_charge_keys"] == ["pmt_000003"]  # kept
    assert stack.behavior["lose_ack_rate"] == 0.0  # and the noise put back
    assert generator.behavior_note == "the provider's behavior was put back as it was found"


def test_noise_settings_someone_else_changed_meanwhile_are_left_alone():
    clock = Clock()
    stack = Stack(clock)
    generator = stack.generator(quiet(seconds=1.0, lose_ack_rate=0.05))
    original_wait = clock.wait

    def wait(seconds):
        stack.behavior = stack.behavior | {"lose_ack_rate": 0.5}  # someone else's choice for the same setting
        original_wait(seconds)

    generator._wait = wait
    generator.run()
    assert stack.behavior["lose_ack_rate"] == 0.5
    assert "left alone" in generator.behavior_note


def test_an_outage_injected_before_the_traffic_stays_through_it_and_after():
    """The fault and the traffic each change only their own settings, in either order."""
    clock = Clock()
    stack = Stack(clock)
    stack.behavior = DEFAULT_BEHAVIOR | {"outage_delay_seconds": 5.0}
    seen = []
    generator = stack.generator(quiet(seconds=1.0, lose_ack_rate=0.05, lost_answer_rate=0.02))
    original_wait = clock.wait
    generator._wait = lambda seconds: seen.append(stack.behavior["outage_delay_seconds"]) or original_wait(seconds)
    generator.run()
    assert seen and set(seen) == {5.0}  # never switched off while the traffic ran
    assert stack.behavior == DEFAULT_BEHAVIOR | {"outage_delay_seconds": 5.0}  # noise gone, outage still there


def test_without_noise_the_providers_behavior_is_never_touched():
    clock = Clock()
    stack = Stack(clock)
    stack.generator(quiet(seconds=1.0)).run()
    assert stack.behavior_puts == []


# Answers ----------------------------------------------------------------------------


def response(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {})


def test_what_may_be_sent_again():
    assert retryable(None) and retryable(response(503)) and retryable(response(500))
    assert retryable(response(409, {"error": {"code": "request_in_progress"}}))
    assert not retryable(response(409, {"error": {"code": "payment_not_settled"}}))
    assert not retryable(response(201)) and not retryable(response(402)) and not retryable(response(422))


def test_what_the_client_learned():
    assert payment_outcome(response(201, {"id": "pmt_1"})) == ("succeeded", {"id": "pmt_1"})
    assert payment_outcome(response(503, {"payment": {"id": "pmt_1"}})) == ("unknown", {"id": "pmt_1"})
    assert payment_outcome(None) == ("unknown", None)
    assert payment_outcome(response(402, {"payment": {"id": "pmt_1"}})) == ("failed", {"id": "pmt_1"})
    assert payment_outcome(response(422)) == ("error", None)
    assert refund_outcome(response(201, {"id": "rfd_1"})) == ("succeeded", {"id": "rfd_1"})
    assert refund_outcome(response(503, {"refund": {"id": "rfd_1"}})) == ("unknown", {"id": "rfd_1"})
    assert refund_outcome(None) == ("unknown", None)
    assert refund_outcome(response(422, {"refund": {"id": "rfd_1"}})) == ("refused", {"id": "rfd_1"})
    assert refund_outcome(response(409, {"error": {"code": "payment_not_settled"}})) == ("refused", None)
    assert refund_outcome(response(404)) == ("error", None)


def test_a_double_click_as_the_pool_shuts_down_is_not_sent_and_its_payment_still_is():
    clock = Clock()
    stack = Stack(clock)
    calls = []

    def submit(task):  # the second submit, from inside the payment, finds the pool shutting down
        calls.append(task)
        if len(calls) == 2:
            raise RuntimeError("cannot schedule new futures after shutdown")
        task()

    generator = stack.generator(quiet(seconds=1.0), submit=submit, payments=one_payment(double_click=True))
    stats = generator.run()
    assert (stats["not_sent.double_clicks"], stats["payments.succeeded"], stats["generator_errors"]) == (1, 1, 0)
    assert generator.in_flight == 0 and len(stack.posts()) == 1


def test_traffic_through_the_real_thread_pool_with_real_waits():
    stack = Stack(Clock())
    stack.clock = lambda: 0.0  # the stack's own log times do not matter here
    settings = quiet(seconds=1.0, rate=20.0, double_click_share=0.3)
    generator = TrafficGenerator(
        settings,
        api=httpx.Client(transport=httpx.MockTransport(stack.api_handler), base_url="http://127.0.0.1:8080"),
        provider=httpx.Client(transport=httpx.MockTransport(stack.provider_handler), base_url="http://127.0.0.1:8081"),
    )
    stats = generator.run()
    assert stats["payments.arrived"] >= 5 and stats["payments.succeeded"] == stats["payments.arrived"]
    assert stats["generator_errors"] == 0 and generator.in_flight == 0
    sent = stats["double_clicks.sent"]
    assert sent == sum(stats[f"double_clicks.{a}"] for a in ("in_progress", "replayed", "ran_first", "other"))


def test_the_generators_requests_never_go_through_a_proxy(monkeypatch):
    """Proxy settings in the environment or the system would send 127.0.0.1 traffic to another machine."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:3128")
    generator = TrafficGenerator(quiet())
    assert generator.api.trust_env is False and generator.provider.trust_env is False


def test_a_client_passed_to_the_generator_must_aim_at_this_mac_too():
    with pytest.raises(ValueError, match="only this Mac"):
        TrafficGenerator(quiet(), api=httpx.Client(base_url="http://payments.example.com"))


def test_the_measurement_says_how_much_traffic_each_check_left_for_the_next_one():
    from klaxon.demo.under_traffic import CheckRun, _line, left_in_total

    at = "2026-10-13T21:00:01.000+00:00"
    busy = CheckRun(
        1, at, 1.0, "passed", [], None, {}, {"provider_events": 4, "provider_charges": 3, "provider_refunds": 2}
    )
    calm = CheckRun(2, at, 0.9, "passed", [], None, {}, {"provider_events": 0, "provider_charges": 0})
    assert _line(busy).endswith("left for the next check: 4 events, 3 charges, 2 refunds")
    assert left_in_total([busy, calm]) == (
        "Newer than their cutoffs, left for the next check: 4 provider events in all, 0 to 4 per check (2 checks)."
    )
    assert left_in_total([]) == "No check left anything for the next one."
