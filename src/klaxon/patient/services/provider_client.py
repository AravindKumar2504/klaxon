"""Our client for the fake provider's API: timeouts, retries that are safe to make, and a circuit breaker.

Every call carries an idempotency key, so a retry after a timeout cannot charge
twice: the provider answers a repeated key with the first result. Errors the
provider will not change its mind about (4xx other than 409 and 429) are not
retried.

The circuit breaker (week 4, the provider-outage fault's remedy). Without one,
when the provider hangs, every payment waits out 3 timed-out attempts, and the
API's worker threads fill up. The breaker fails fast after repeated failures,
and tries again later:

    closed     every attempt goes to the provider. A call fails when none of its
               attempts got an answer from it: each timed out or could not
               connect; got a 5xx other than 500 and 501 (the provider, or a
               gateway in front of it, cannot serve: 502, 503, 504, a CDN's
               52x, 530); or got a 500 or 501 only after more than half the
               timeout (it held a worker thread for most of a timeout). Any
               other answer counts as one, a quick 500 included: the provider
               is there. It sets the count of failed calls in a row back to 0.
               At 5 failed calls in a row, the breaker opens.
    open       no attempt goes out: each call fails at once (CircuitOpen, a
               ProviderUnavailable, so the API leaves the payment pending as
               before), for 10 s.
    half-open  then one attempt goes out, a trial. If the provider answers it,
               the breaker closes; if not, it opens again for 10 s more. Other
               calls still fail at once while the trial is out.

The breaker asks one question: is the provider there? Whether a call worked
is the client's question, and its answer is unchanged: a 500 still means the
outcome is unknown, so the payment stays pending. But a quick 500 holds no
worker thread, and the traffic's noise (a charge whose answer the provider
loses on purpose) is a quick 500 at every attempt with its key, and again at
each retry of the client. Counted as failures, such requests can open the
breaker while the provider is up: with 20% of answers lost at 5 payments per
second and the known-bad confirmer, it was not closed about half the time, by
the review's simulation. The limits: a provider that answers quick 500s to
every call never opens it (its calls fail fast anyway); one that answers 409
or 429, which the client retries, never does either, even slowly, though a
slow one holds a thread (this fake provider sends neither slowly); and in a
partial outage, where some calls hang and others get quick 500s, each 500
starts the count again.

Only the trial decides: a result counts only while the breaker is in the
state that let its attempt go, so an attempt sent before the breaker opened,
which fails after, changes nothing. The API's worker threads share one
breaker, behind a lock. PROVIDER_BREAKER=off runs without it, the old
behavior, kept as a known-bad setting (klaxon.patient.deploys).

Telemetry: klaxon.provider.attempts counts every attempt by its result, an
HTTP status or "no_answer" (with the error's type). The httpx instrumentation
records a call's duration only when an answer came back, so without this
count a provider that times out would not show in the metrics at all.
klaxon.provider.refused counts the calls the breaker refused: the attempt it
refused never reached the provider (a call refused at its second or third
attempt did send the ones before). The breaker logs each change of its state.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import httpx
from opentelemetry import metrics

from klaxon.patient.services.common import log

logger = logging.getLogger("klaxon.provider_client")

RETRYABLE_STATUSES = {409, 429}  # "a request with this key is still running" and "slow down"
OWN_ERRORS = {500, 501}  # 5xx that are the provider's own answer about one request; any other 5xx is no answer
SLOW_SHARE = 0.5  # of the timeout: a 500 or 501 that took longer held a thread for most of a timeout: no answer
BREAKER_FAILURES = 5  # failed calls in a row that open the breaker
BREAKER_OPEN_SECONDS = 10.0  # how long it stays open before a trial attempt
CLOSED, OPEN, HALF_OPEN = "closed", "open", "half-open"
meter = metrics.get_meter("klaxon.provider_client")
ATTEMPTS = meter.create_counter(
    "klaxon.provider.attempts",
    unit="{attempt}",
    description="Every call to the provider, retries included, by result: an HTTP status, or no_answer.",
)
REFUSED = meter.create_counter(
    "klaxon.provider.refused",
    unit="{call}",
    description="Calls the circuit breaker refused: the attempt it refused never reached the provider.",
)


class ProviderUnavailable(Exception):
    """No answer we can trust (timeouts, connection errors, 5xx), even after retries. The outcome is unknown."""


class CircuitOpen(ProviderUnavailable):
    """The circuit breaker refused the call: the provider failed too often, so it was not asked this time. An earlier
    attempt with the same key may have reached it, so the outcome is still unknown."""


class ProviderRejected(Exception):
    """The provider answered and refused (4xx). The outcome is known: nothing was charged."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class CircuitBreaker:
    """Fail fast after repeated failures, and try again later (the module docstring has its three states)."""

    def __init__(
        self,
        failures: int = BREAKER_FAILURES,
        open_seconds: float = BREAKER_OPEN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failures = failures
        self.open_seconds = open_seconds
        self.clock = clock
        self.state = CLOSED
        self._lock = threading.Lock()
        self._in_a_row = 0  # failed calls in a row, while closed
        self._opened_at = 0.0
        self._period = 0  # one more at every change of state: an attempt's result counts only in the period it began

    def admit(self) -> int | None:
        """Leave to send one attempt now, as the period it begins in; None when the call must fail at once."""
        with self._lock:
            if self.state == OPEN and self.clock() - self._opened_at >= self.open_seconds:
                self._change(HALF_OPEN, logging.INFO, "circuit breaker half-open: one trial call to the provider")
                return self._period  # this attempt is the trial
            return self._period if self.state == CLOSED else None

    def attempted(self, period: int, answered: bool) -> None:
        """What came of one attempt admit() let go: answered by the provider (the module docstring says what counts),
        or not. Closed, an answer sets the count of failed calls back to 0; half-open, the trial's result decides."""
        with self._lock:
            if period != self._period:
                return  # it began in a state that has changed since
            if self.state == HALF_OPEN:
                if answered:
                    self._change(CLOSED, logging.INFO, "circuit breaker closed: the provider answered the trial call")
                else:
                    self._open("circuit breaker open again: the trial call failed")
            elif answered:
                self._in_a_row = 0

    def failed(self, period: int) -> None:
        """A call none of whose attempts got an answer from the provider. At `failures` in a row, closed, it opens."""
        with self._lock:
            if period != self._period:
                return  # its last attempt began in a state that has changed since: only a closed one counts
            self._in_a_row += 1
            if self._in_a_row >= self.failures:
                self._open(f"circuit breaker open: {self._in_a_row} provider calls in a row got no answer")

    def why(self) -> str:
        """Why a call fails at once, in a few words."""
        with self._lock:
            if self.state == CLOSED:  # its trial got an answer just after this call was refused
                return "the circuit breaker was open, and has just closed"
            if self.state == HALF_OPEN:
                return "the circuit breaker is half-open, and its trial call is still out"
            left = max(0.0, self.open_seconds - (self.clock() - self._opened_at))
            return f"the circuit breaker is open (the next trial call in {left:.1f} s)"

    def _open(self, message: str) -> None:
        self._opened_at = self.clock()
        self._change(OPEN, logging.WARNING, f"{message}; calls fail at once for {self.open_seconds:g} s")

    def _change(self, state: str, level: int, message: str) -> None:
        self.state = state
        self._period += 1
        self._in_a_row = 0
        log(logger, level, message, breaker=state)


def breaker_setting(value: str) -> CircuitBreaker | None:
    """PROVIDER_BREAKER: on (the default), or off, the old behavior, known to be bad: every call waits out its
    timeouts."""
    if value == "on":
        return CircuitBreaker()
    if value == "off":
        return None
    raise RuntimeError(f"PROVIDER_BREAKER must be on or off, not {value!r}")


class ProviderClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 3.0,
        attempts: int = 3,
        backoff_seconds: float = 0.2,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if attempts < 1:
            raise ValueError("a provider client makes at least one attempt per call")
        self.http = http or httpx.Client(base_url=base_url, timeout=timeout_seconds)
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        self.breaker = breaker  # None: no breaker (PROVIDER_BREAKER=off)
        self.slow_seconds = timeout_seconds * SLOW_SHARE
        self.clock = clock  # times each attempt, for the breaker

    def create_payment(
        self, customer: str, amount_cents: int, currency: str, idempotency_key: str, metadata: dict[str, str]
    ) -> dict:
        body = {"customer": customer, "amount_cents": amount_cents, "currency": currency, "metadata": metadata}
        return self._post("/v1/payments", body, idempotency_key)

    def create_refund(
        self, payment_id: str, amount_cents: int, idempotency_key: str, metadata: dict[str, str] | None = None
    ) -> dict:
        body = {"payment_id": payment_id, "amount_cents": amount_cents, "metadata": metadata or {}}
        return self._post("/v1/refunds", body, idempotency_key)

    def _post(self, path: str, body: dict, idempotency_key: str) -> dict:
        problem = "no attempt made"
        heard = False  # did any attempt get an answer from the provider, for the breaker: then the call did not fail
        for attempt in range(1, self.attempts + 1):
            period = 0 if self.breaker is None else self.breaker.admit()
            if period is None:
                REFUSED.add(1, {"path": path})
                before = f"; before it, {attempt - 1} attempt(s) failed: {problem}" if attempt > 1 else ""
                raise CircuitOpen(f"{path} not sent: {self.breaker.why()}{before}")
            answered = False
            started = self.clock()
            try:
                response = self.http.post(path, json=body, headers={"Idempotency-Key": idempotency_key})
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__}: {exc}"
                ATTEMPTS.add(1, {"path": path, "result": "no_answer", "error": type(exc).__name__})
            else:
                status, took = response.status_code, self.clock() - started
                answered = status < 500 or (status in OWN_ERRORS and took <= self.slow_seconds)
                heard = heard or answered
                ATTEMPTS.add(1, {"path": path, "result": str(response.status_code)})
                if response.status_code < 300:
                    return response.json()
                if response.status_code < 500 and response.status_code not in RETRYABLE_STATUSES:
                    error = _error_of(response)
                    raise ProviderRejected(
                        response.status_code, error.get("code", "rejected"), error.get("message", "")
                    )
                problem = f"HTTP {response.status_code}"
            finally:
                if self.breaker is not None:  # whatever happened, so a trial can never be left out forever
                    self.breaker.attempted(period, answered)
            if attempt < self.attempts and (self.breaker is None or self.breaker.state == CLOSED):
                self.sleep(self.backoff_seconds * 2 ** (attempt - 1))  # not once it opened: the next attempt fails now
        if self.breaker is not None and not heard:
            self.breaker.failed(period)  # no attempt got an answer from the provider: a failed call
        raise ProviderUnavailable(f"{path} failed after {self.attempts} attempts: {problem}")


def _error_of(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}
