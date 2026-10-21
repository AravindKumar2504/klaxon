"""The provider client's circuit breaker: it fails fast after repeated failures, and tries again later (week 4).

The clock is made up and moved by hand, and the provider is httpx's MockTransport: nothing waits, nothing is sent."""

import logging
import threading

import httpx
import pytest

from klaxon.patient.services.provider_client import (
    BREAKER_FAILURES,
    BREAKER_OPEN_SECONDS,
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitOpen,
    ProviderClient,
    ProviderRejected,
    ProviderUnavailable,
    breaker_setting,
)


class Clock:
    """A clock the test moves by hand: no real waiting."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def failed_calls(breaker: CircuitBreaker, n: int = 1, attempts: int = 3) -> None:
    """n calls, each of whose attempts got no answer we can trust."""
    for _ in range(n):
        for _ in range(attempts):
            period = breaker.admit()
            breaker.attempted(period, answered=False)
        breaker.failed(period)


def answered(breaker: CircuitBreaker) -> None:
    breaker.attempted(breaker.admit(), answered=True)


def opened(clock: Clock) -> CircuitBreaker:
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=clock)
    failed_calls(breaker, 5)
    assert breaker.state == OPEN
    return breaker


def test_the_default_breaker_opens_after_5_failed_calls_in_a_row_and_tries_again_after_10_s():
    assert (BREAKER_FAILURES, BREAKER_OPEN_SECONDS) == (5, 10.0)  # Aravind confirmed these, 2026-10-21
    breaker = breaker_setting("on")
    assert (breaker.failures, breaker.open_seconds, breaker.state) == (5, 10.0, CLOSED)


def test_off_is_no_breaker_and_anything_else_is_refused():
    assert breaker_setting("off") is None  # the old behavior, a known-bad setting
    with pytest.raises(RuntimeError, match="must be on or off"):
        breaker_setting("yes")


def test_it_opens_at_the_fifth_failed_call_in_a_row_not_before():
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=Clock())
    failed_calls(breaker, 4)
    assert breaker.state == CLOSED and breaker.admit() is not None
    failed_calls(breaker)
    assert breaker.state == OPEN and breaker.admit() is None


def test_it_counts_calls_not_attempts():
    """A charge whose answer the provider loses on purpose gets a 500 at each of its attempts: one failed call."""
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=Clock())
    for _ in range(14):  # 14 failed attempts, all in a row, but not one failed call yet
        breaker.attempted(breaker.admit(), answered=False)
    assert breaker.state == CLOSED
    failed_calls(breaker, 4)
    assert breaker.state == CLOSED


def test_any_answer_sets_the_count_back_to_zero():
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=Clock())
    failed_calls(breaker, 4)
    answered(breaker)
    failed_calls(breaker, 4)
    assert breaker.state == CLOSED  # 8 failed calls, but never 5 in a row


def test_open_it_refuses_every_call_until_its_time_is_up():
    clock = Clock()
    breaker = opened(clock)
    clock.now += 9.99
    assert breaker.admit() is None and breaker.state == OPEN
    assert breaker.why() == "the circuit breaker is open (the next trial call in 0.0 s)"
    clock.now += 0.01
    assert breaker.admit() is not None and breaker.state == HALF_OPEN  # that one is the trial


def test_half_open_only_the_trial_goes_out():
    clock = Clock()
    breaker = opened(clock)
    clock.now += 10
    assert breaker.admit() is not None
    assert breaker.admit() is None and breaker.admit() is None  # the others still fail at once
    assert breaker.why() == "the circuit breaker is half-open, and its trial call is still out"


def test_a_trial_that_gets_an_answer_closes_it():
    clock = Clock()
    breaker = opened(clock)
    clock.now += 10
    answered(breaker)
    assert breaker.state == CLOSED and breaker.admit() is not None
    failed_calls(breaker, 4)
    assert breaker.state == CLOSED  # and the count starts again from zero


def test_a_trial_that_fails_opens_it_again_for_another_10_s():
    clock = Clock()
    breaker = opened(clock)
    clock.now += 12
    trial = breaker.admit()
    clock.now += 3  # a trial that waits out its 3 s timeout
    breaker.attempted(trial, answered=False)
    assert breaker.state == OPEN
    clock.now += 9.99
    assert breaker.admit() is None
    clock.now += 0.01
    assert breaker.admit() is not None and breaker.state == HALF_OPEN


def test_a_result_from_before_a_change_of_state_changes_nothing():
    """Only the trial decides: an attempt let go while closed, which comes back after the breaker opened, does not
    close it, and a call that fails while the trial is out does not open it again."""
    clock = Clock()
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=clock)
    early, late = breaker.admit(), breaker.admit()
    failed_calls(breaker, 5)
    breaker.attempted(early, answered=True)
    assert breaker.state == OPEN
    clock.now += 10
    trial = breaker.admit()
    breaker.attempted(late, answered=False)
    breaker.failed(late)
    assert breaker.state == HALF_OPEN
    breaker.attempted(trial, answered=True)
    assert breaker.state == CLOSED
    breaker.failed(early)  # a call from before it all ends: it began in another period
    failed_calls(breaker, 4)
    assert breaker.state == CLOSED


def test_many_threads_at_once_get_one_trial():
    clock = Clock()
    breaker = opened(clock)
    clock.now += 10
    start = threading.Barrier(16)
    let_go = []

    def ask():
        start.wait()
        let_go.append(breaker.admit())

    threads = [threading.Thread(target=ask) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(let_go) == 16 and sum(period is not None for period in let_go) == 1


def test_each_change_of_state_is_logged_once(caplog):
    caplog.set_level(logging.INFO, logger="klaxon.provider_client")
    clock = Clock()
    breaker = opened(clock)
    breaker.admit()  # refused: no change, no line
    clock.now += 10
    breaker.attempted(breaker.admit(), answered=False)
    clock.now += 10
    answered(breaker)
    said = [(record.levelname, record.getMessage(), record.fields["breaker"]) for record in caplog.records]
    assert said == [
        ("WARNING", "circuit breaker open: 5 provider calls failed in a row; calls fail at once for 10 s", "open"),
        ("INFO", "circuit breaker half-open: one trial call to the provider", "half-open"),
        ("WARNING", "circuit breaker open again: the trial call failed; calls fail at once for 10 s", "open"),
        ("INFO", "circuit breaker half-open: one trial call to the provider", "half-open"),
        ("INFO", "circuit breaker closed: the provider answered the trial call", "closed"),
    ]


# The breaker in the provider client


def client_with(handler, breaker):
    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://provider")
    client = ProviderClient("http://provider", attempts=3, http=http, sleep=sleeps.append, breaker=breaker)
    return client, sleeps


def hanging(seen):
    def handler(request):
        seen.append(request.url.path)
        raise httpx.ReadTimeout("timed out")

    return handler


def test_once_open_a_call_fails_at_once_and_never_reaches_the_provider(telemetry):
    seen = []
    clock = Clock()
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=clock)
    client, sleeps = client_with(hanging(seen), breaker)
    before = [telemetry.counter("klaxon.provider.refused", path=path) for path in ("/v1/payments", "/v1/refunds")]
    for n in range(5):  # each waits out its 3 attempts; the fifth opens the breaker
        with pytest.raises(ProviderUnavailable) as failed:
            client.create_payment("cus", 100, "usd", f"pmt_00000{n}", {})
        assert not isinstance(failed.value, CircuitOpen)
    assert breaker.state == OPEN and len(seen) == 15 and len(sleeps) == 10
    with pytest.raises(CircuitOpen) as refused:
        client.create_refund("pay_1", 100, "rfd_000001")
    assert len(seen) == 15 and len(sleeps) == 10  # nothing sent, no wait
    assert str(refused.value) == "/v1/refunds not sent: the circuit breaker is open (the next trial call in 10.0 s)"
    with pytest.raises(CircuitOpen):
        client.create_payment("cus", 100, "usd", "pmt_000009", {})
    after = [telemetry.counter("klaxon.provider.refused", path=path) for path in ("/v1/payments", "/v1/refunds")]
    assert [b - a for a, b in zip(before, after, strict=True)] == [1, 1]  # each refused call, counted


def test_a_call_whose_trial_fails_stops_at_once_and_says_what_failed_before():
    clock = Clock()
    breaker = opened(clock)
    clock.now += 10
    seen = []
    client, sleeps = client_with(hanging(seen), breaker)
    with pytest.raises(CircuitOpen) as refused:
        client.create_payment("cus", 100, "usd", "pmt_000001", {})
    assert len(seen) == 1 and breaker.state == OPEN  # the trial, and no other attempt
    assert str(refused.value).endswith("; before it, 1 attempt(s) failed: ReadTimeout: timed out")


def test_without_a_breaker_every_call_waits_out_all_its_attempts():
    """PROVIDER_BREAKER=off: the old behavior, known to be bad."""
    seen = []
    client, _ = client_with(hanging(seen), breaker=None)
    for n in range(8):
        with pytest.raises(ProviderUnavailable) as failed_call:
            client.create_payment("cus", 100, "usd", f"pmt_00000{n}", {})
        assert not isinstance(failed_call.value, CircuitOpen)
    assert len(seen) == 24


def test_5xx_answers_count_as_failures():
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=Clock())
    client, _ = client_with(lambda request: httpx.Response(503), breaker)
    for n in range(5):
        with pytest.raises(ProviderUnavailable):
            client.create_payment("cus", 100, "usd", f"pmt_00000{n}", {})
    assert breaker.state == OPEN


def test_lost_answers_among_answered_calls_never_open_it():
    """The traffic's noise: now and then the provider charges, then answers 500 to every attempt with that key."""
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=Clock())

    def handler(request):
        key = request.headers["idempotency-key"]
        return httpx.Response(500) if key.endswith("lost") else httpx.Response(200, json={"id": "pay_1"})

    client, _ = client_with(handler, breaker)
    for n in range(40):
        lost = n % 2 == 0  # every other call: far more than the 2% the traffic loses
        if lost:
            with pytest.raises(ProviderUnavailable):
                client.create_payment("cus", 100, "usd", f"pmt_{n:06d}_lost", {})
        else:
            client.create_payment("cus", 100, "usd", f"pmt_{n:06d}", {})
    assert breaker.state == CLOSED


@pytest.mark.parametrize("status", [400, 402, 404])
def test_a_refusal_is_an_answer_it_never_opens_the_breaker(status):
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=Clock())
    failed_calls(breaker, 4)
    client, _ = client_with(lambda request: httpx.Response(status, json={"error": {"code": "no"}}), breaker)
    with pytest.raises(ProviderRejected):
        client.create_refund("pay_1", 100, "rfd_000001")
    failed_calls(breaker, 4)
    assert breaker.state == CLOSED


@pytest.mark.parametrize("status", [409, 429])
def test_busy_and_slow_down_are_answers_too(status):
    breaker = CircuitBreaker(failures=5, open_seconds=10.0, clock=Clock())
    failed_calls(breaker, 4)
    answers = [httpx.Response(status), httpx.Response(200, json={"id": "re_1"})]
    client, _ = client_with(lambda request: answers.pop(0), breaker)
    assert client.create_refund("pay_1", 100, "rfd_000001") == {"id": "re_1"}
    failed_calls(breaker, 4)
    assert breaker.state == CLOSED


def test_a_trial_that_crashes_still_counts_so_the_breaker_is_never_stuck_half_open():
    clock = Clock()
    breaker = opened(clock)
    clock.now += 10

    def crashing(request):
        raise httpx.DecodingError("a body that cannot be read")  # not a transport error: it is not caught

    client, _ = client_with(crashing, breaker)
    with pytest.raises(httpx.DecodingError):
        client.create_payment("cus", 100, "usd", "pmt_000001", {})
    assert breaker.state == OPEN  # the trial counted as failed: it tries again in 10 s, not never
    clock.now += 10
    assert breaker.admit() is not None


def test_after_the_provider_recovers_the_trial_closes_it_and_calls_go_out_again():
    clock = Clock()
    breaker = opened(clock)
    seen = []

    def answering(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"id": "pay_000009"})

    client, _ = client_with(answering, breaker)
    with pytest.raises(CircuitOpen):
        client.create_payment("cus", 100, "usd", "pmt_000001", {})
    clock.now += 10
    assert client.create_payment("cus", 100, "usd", "pmt_000001", {}) == {"id": "pay_000009"}  # the trial
    assert breaker.state == CLOSED
    assert client.create_payment("cus", 100, "usd", "pmt_000002", {}) == {"id": "pay_000009"}
    assert len(seen) == 2
