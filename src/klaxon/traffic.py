"""Synthetic traffic for the patient: customers paying, and sometimes asking for a refund, at a steady rate.

A test harness: not part of the patient and not the Klaxon agent. Synthetic data only (made-up
customers and amounts, the fake provider, no real money), and it only talks to this Mac's stack.

One seeded plan: the same seed and rate give the same customers, amounts, gaps and choices.
- Payments arrive at random, like real customers (a Poisson process), --rate per second on average.
  Amounts are mostly $10 to $150, a few up to $2,000.
- About 1 in 50 is sent twice at once, like a double click (the same Idempotency-Key).
- About 1 in 10 is refunded a few seconds later, in full or in half.
- Retries like a careful client: after a 503 (outcome unknown), a 409 (still running), a 5xx or no
  answer at all, the same request with the same key, after 0.5, 1 and 2 s. Then it gives up, and
  asks for the payment's status (or the refund's) 5 s and 60 s later, the way a shop checks an order
  it is unsure of.

It also turns on two kinds of noise at the fake provider (seeded): acknowledgements lost on purpose,
so some webhooks arrive twice (lose_ack_rate), and charges and refunds whose answer is lost
(lost_answer_rate, the provider's fail_after_charge_rate and fail_after_refund_rate). A healthy stack
absorbs both: the ledger ignores the repeats, and the payment confirmer settles the payments and
refunds whose answer was lost. It changes only those settings and keeps the rest (a fault's outage,
say); when it stops, it puts them back as it found them, unless something else changed them meanwhile.

Open loop: customers keep arriving when the stack slows down, as real ones do. At most
--max-in-flight requests run at once; an arrival past that is counted as shed and not sent.

Run: python -m klaxon.traffic --rate 2 --seconds 60    (or: make traffic; Ctrl-C stops it)
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import random
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import httpx

API_URL = "http://127.0.0.1:8080"
PROVIDER_URL = "http://127.0.0.1:8081"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
RETRY_DELAYS = (0.5, 1.0, 2.0)  # seconds before each retry; then the client gives up
STATUS_CHECKS = (5.0, 60.0)  # seconds after giving up: did it go through after all?
CUSTOMERS = 500
MEDIAN_CENTS, SPREAD = 4_000, 0.8  # log-normal amounts around $40
MIN_CENTS, MAX_CENTS = 100, 200_000
MAX_RATE = 100.0  # payments per second; more would only measure this Mac
REPLAYED_HEADER = "Idempotent-Replayed"


def require_local(url: str) -> None:
    """Klaxon only acts on the local demo system: refuse any other address."""
    host = urlsplit(url).hostname
    if host not in LOCAL_HOSTS:
        raise ValueError(f"refusing to send traffic to {url!r}: only this Mac ({', '.join(LOCAL_HOSTS)})")


@dataclass(frozen=True)
class Settings:
    rate: float = 2.0  # new payments per second, on average
    seconds: float | None = None  # how long to send; None: until stopped
    seed: int = 7
    lose_ack_rate: float = 0.05
    lost_answer_rate: float = 0.02
    double_click_share: float = 0.02
    refund_share: float = 0.10
    max_in_flight: int = 64
    api_url: str = API_URL
    provider_url: str = PROVIDER_URL
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])  # in every key: runs never collide

    def __post_init__(self) -> None:
        if not 0 < self.rate <= MAX_RATE:
            raise ValueError(f"rate must be above 0 and at most {MAX_RATE:g} payments per second")
        if self.seconds is not None and self.seconds <= 0:
            raise ValueError("seconds must be above 0")
        for name in ("lose_ack_rate", "lost_answer_rate", "double_click_share", "refund_share"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if not 1 <= self.max_in_flight <= 256:
            raise ValueError("max_in_flight must be between 1 and 256")
        require_local(self.api_url)
        require_local(self.provider_url)

    @property
    def noise(self) -> dict:
        """The fake provider's behavior while the traffic runs."""
        return {
            "lose_ack_rate": self.lose_ack_rate,
            "fail_after_charge_rate": self.lost_answer_rate,
            "fail_after_refund_rate": self.lost_answer_rate,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class PlannedPayment:
    n: int
    at: float  # seconds after the start
    customer: str
    amount_cents: int
    double_click: bool
    refund_after: float | None  # seconds after it succeeds; None: never refunded
    refund_in_full: bool

    @property
    def refund_cents(self) -> int:
        return self.amount_cents if self.refund_in_full else self.amount_cents // 2


def plan(
    seed: int, rate: float, double_click_share: float = 0.02, refund_share: float = 0.10
) -> Iterator[PlannedPayment]:
    """The customers, endlessly. Every payment takes the same seven draws, so one choice never shifts the rest."""
    rng = random.Random(seed)
    at = 0.0
    for n in itertools.count(1):
        at += rng.expovariate(rate)
        customer = f"cus_{rng.randrange(1, CUSTOMERS + 1):03d}"
        amount = min(MAX_CENTS, max(MIN_CENTS, round(rng.lognormvariate(math.log(MEDIAN_CENTS), SPREAD))))
        double_click = rng.random() < double_click_share
        refunded = rng.random() < refund_share
        refund_after = rng.uniform(2.0, 20.0)
        in_full = rng.random() < 0.5
        yield PlannedPayment(n, at, customer, amount, double_click, refund_after if refunded else None, in_full)


def retryable(response: httpx.Response | None) -> bool:
    """No answer, a 5xx, or "a request with this key is still running": the same request may be sent again."""
    if response is None or response.status_code >= 500:
        return True
    return response.status_code == 409 and _error_code(response) == "request_in_progress"


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error")
    except ValueError:
        return None
    return error.get("code") if isinstance(error, dict) else None


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def payment_outcome(response: httpx.Response | None) -> tuple[str, dict | None]:
    """What the client learned: succeeded, failed (refused), unknown (it may have gone through), or error."""
    if response is None:
        return "unknown", None
    body = _json(response)
    if response.status_code == 201:
        return "succeeded", body
    if response.status_code == 402:
        return "failed", body.get("payment")
    if response.status_code >= 500 or retryable(response):
        return "unknown", body.get("payment")  # a 503 still names our payment, so its status can be asked later
    return "error", None


def refund_outcome(response: httpx.Response | None) -> tuple[str, dict | None]:
    """As for a payment: succeeded, refused, unknown or error, and the refund when the answer names one."""
    if response is None:
        return "unknown", None
    body = _json(response)
    if response.status_code >= 500 or retryable(response):
        return "unknown", body.get("refund")  # a 503 still names our refund, so its status can be asked later
    if response.status_code == 201:
        return "succeeded", body
    if response.status_code in (409, 422):
        return "refused", body.get("refund")
    return "error", None


class Stats:
    """Counters shared by every request thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self.unexpected: list[str] = []  # the first few answers the generator did not expect

    def add(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] += n

    def note(self, what: str) -> None:
        with self._lock:
            if len(self.unexpected) < 20:
                self.unexpected.append(what[:300])

    def __getitem__(self, name: str) -> int:
        with self._lock:
            return self._counts[name]

    def to_dict(self) -> dict:
        with self._lock:
            return {"counts": dict(sorted(self._counts.items())), "unexpected": list(self.unexpected)}


class TrafficGenerator:
    def __init__(
        self,
        settings: Settings,
        api: httpx.Client | None = None,
        provider: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        submit: Callable[[Callable[[], None]], object] | None = None,
        wait: Callable[[float], None] | None = None,
        payments: Iterable[PlannedPayment] | None = None,
    ) -> None:
        self.settings = settings
        self._payments = payments  # None: the seeded plan; tests pass their own
        limits = httpx.Limits(max_connections=settings.max_in_flight, max_keepalive_connections=settings.max_in_flight)
        # trust_env=False: no proxy from the environment or the system settings, so nothing leaves this Mac.
        self.api = api or httpx.Client(base_url=settings.api_url, timeout=15.0, limits=limits, trust_env=False)
        self.provider = provider or httpx.Client(base_url=settings.provider_url, timeout=10.0, trust_env=False)
        for client in (self.api, self.provider):
            require_local(str(client.base_url))  # a client passed in must aim at this Mac too
        self.clock = clock
        self.stats = Stats()
        self._submit = submit  # None: a thread pool; tests pass one that runs each task at once
        self._wait = wait  # None: sleep until woken; tests pass one that moves their clock
        self._closing = threading.Event()  # no new payments, refunds or status checks
        self._stopping = threading.Event()  # and give up on retries too
        self._lock = threading.Lock()
        self._later: list[tuple[float, int, Callable, tuple]] = []  # follow-ups: (due, order, task, args)
        self._order = itertools.count()
        self._in_flight = 0
        self._start = 0.0
        self._previous_behavior: dict | None = None
        self._noise: dict | None = None
        self._thread: threading.Thread | None = None
        self.behavior_note = "the provider's behavior was not changed"

    # Starting and stopping --------------------------------------------------------

    def run(self) -> Stats:
        """Send traffic until --seconds pass or close() is called, in this thread."""
        self._noise_on()
        return self._serve()

    def start(self) -> None:
        """Send traffic in a background thread. Errors reaching the provider show up here, before any traffic."""
        self._noise_on()
        self._thread = threading.Thread(target=self._serve, name="traffic", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Send nothing new. Requests already running finish, retries included."""
        self._closing.set()

    def stop_now(self) -> None:
        """Send nothing new, and give up on retries."""
        self._stopping.set()
        self._closing.set()

    def wait(self, timeout: float | None = None) -> bool:
        """True once the traffic has stopped and the provider's behavior is back."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def join(self) -> Stats:
        self.wait()
        return self.stats

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def _noise_on(self) -> None:
        """Set the noise's own fields, and keep the rest of the provider's behavior (a fault's outage, say)."""
        if not (self.settings.lose_ack_rate or self.settings.lost_answer_rate):
            return  # no noise asked for: leave the provider's behavior as it is
        found = self.provider.get("/admin/behavior").raise_for_status().json()
        self._previous_behavior = {name: found.get(name) for name in self.settings.noise}
        self.provider.put("/admin/behavior", json=found | self.settings.noise).raise_for_status()
        self._noise = dict(self.settings.noise)

    def _noise_back(self) -> None:
        """Put the noise's fields back as they were found, if they still hold the noise; never touch the rest."""
        if self._noise is None:
            return
        try:
            now = self.provider.get("/admin/behavior").raise_for_status().json()
            if any(now.get(name) != value for name, value in self._noise.items()):
                self.behavior_note = "something else changed the noise's settings meanwhile, so they were left alone"
                return
            back = {name: value for name, value in (self._previous_behavior or {}).items() if value is not None}
            self.provider.put("/admin/behavior", json=now | back).raise_for_status()
            self.behavior_note = "the provider's behavior was put back as it was found"
        except httpx.HTTPError as exc:
            self.behavior_note = f"could not put the provider's behavior back: {exc}"[:300]

    # The schedule -----------------------------------------------------------------

    def _serve(self) -> Stats:
        pool = None
        submit = self._submit
        if submit is None:
            pool = ThreadPoolExecutor(max_workers=self.settings.max_in_flight, thread_name_prefix="traffic")
            submit = pool.submit
        self._submit = submit
        try:
            self._schedule()
        finally:
            self._closing.set()
            if pool is not None:
                pool.shutdown(wait=True)  # the requests already running finish first
            with self._lock:
                for _, _, task, _ in self._later:  # follow-ups the run ended before
                    self.stats.add(f"not_sent.{'refunds' if task == self._refund else 'status_checks'}")
                self._later.clear()
            self._noise_back()
        return self.stats

    def _elapsed(self) -> float:
        return self.clock() - self._start

    def _schedule(self) -> None:
        self._start = self.clock()
        s = self.settings
        payments = (
            iter(self._payments)
            if self._payments is not None
            else plan(s.seed, s.rate, s.double_click_share, s.refund_share)
        )
        upcoming = next(payments, None)
        while not self._closing.is_set():
            now = self._elapsed()
            if s.seconds is not None and now >= s.seconds:
                return
            follow_up = self._due_follow_up(now)
            if follow_up is not None:
                task, args = follow_up
                self._begin(task, *args)
            elif upcoming is not None and upcoming.at <= now:
                self.stats.add("payments.arrived")
                self._begin(self._pay, upcoming)
                upcoming = next(payments, None)
            else:
                with self._lock:
                    next_follow_up = self._later[0][0] if self._later else math.inf
                pause = min(upcoming.at if upcoming is not None else math.inf, next_follow_up) - now
                if s.seconds is not None:
                    pause = min(pause, s.seconds - now)
                self._pause(min(pause, 0.25))

    def _pause(self, seconds: float) -> None:
        if self._wait is not None:
            self._wait(seconds)
        else:
            self._closing.wait(seconds)

    def _due_follow_up(self, now: float) -> tuple[Callable, tuple] | None:
        with self._lock:
            if self._later and self._later[0][0] <= now:
                _, _, task, args = heapq.heappop(self._later)
                return task, args
        return None

    def _follow_up(self, after_seconds: float, task: Callable, *args: object) -> None:
        with self._lock:
            heapq.heappush(self._later, (self._elapsed() + after_seconds, next(self._order), task, args))

    def _begin(self, task: Callable, *args: object) -> None:
        with self._lock:
            if self._in_flight >= self.settings.max_in_flight:
                self.stats.add("shed")
                return
            self._in_flight += 1

        def run() -> None:
            try:
                task(*args)
            except Exception as exc:  # a bug in the generator, not an answer from the stack
                self.stats.add("generator_errors")
                self.stats.note(f"{task.__name__}: {exc!r}")
            finally:
                with self._lock:
                    self._in_flight -= 1

        try:
            self._submit(run)
        except RuntimeError:  # the pool is shutting down: a double click as the run ends is simply not sent
            with self._lock:
                self._in_flight -= 1
            self.stats.add(f"not_sent.{'double_clicks' if task == self._click_again else task.__name__}")

    # What a customer does ---------------------------------------------------------

    def _send(self, path: str, body: dict, key: str) -> httpx.Response | None:
        """POST with the same Idempotency-Key until the answer is final, or the client gives up."""
        response = None
        for delay in (*RETRY_DELAYS, None):
            try:
                response = self.api.post(path, json=body, headers={"Idempotency-Key": key})
            except httpx.HTTPError:
                response = None
            if not retryable(response) or delay is None or self._stopping.is_set():
                return response
            self.stats.add("retries")
            self.stats.add(f"retries.after_{'no_answer' if response is None else response.status_code}")
            if response is not None and response.status_code == 409:
                delay = max(delay, float(response.headers.get("Retry-After", 0) or 0))
            if self._wait is not None:
                self._wait(delay)
            elif self._stopping.wait(delay):
                return response
        return response

    def _pay(self, planned: PlannedPayment) -> None:
        key = f"traffic-{self.settings.run_id}-pay-{planned.n}"
        body = {"customer": planned.customer, "amount_cents": planned.amount_cents}
        if planned.double_click:
            self.stats.add("double_clicks.sent")
            self._begin(self._click_again, key, body)
        outcome, payment = payment_outcome(self._send("/payments", body, key))
        self.stats.add(f"payments.{outcome}")
        if outcome == "error":
            self.stats.note(f"payment {planned.n}: an answer the generator did not expect")
        if self._closing.is_set():
            return
        if outcome == "succeeded" and planned.refund_after is not None:
            self._follow_up(planned.refund_after, self._refund, planned, payment["id"])
        elif outcome == "unknown" and payment and isinstance(payment.get("id"), str):
            self._follow_up(STATUS_CHECKS[0], self._check_status, payment["id"], 0)

    def _click_again(self, key: str, body: dict) -> None:
        """The second click: the same request at once, never retried."""
        try:
            response = self.api.post("/payments", json=body, headers={"Idempotency-Key": key})
        except httpx.HTTPError:
            self.stats.add("double_clicks.no_answer")
            return
        if response.status_code == 409:
            self.stats.add("double_clicks.in_progress")  # the first click is still running
        elif response.headers.get(REPLAYED_HEADER) == "true":
            self.stats.add("double_clicks.replayed")  # the first click's answer, saved
        elif response.status_code == 201:
            self.stats.add("double_clicks.ran_first")  # this click won the race; the other one waits
        else:
            self.stats.add("double_clicks.other")

    def _refund(self, planned: PlannedPayment, payment_id: str) -> None:
        key = f"traffic-{self.settings.run_id}-refund-{planned.n}"
        response = self._send(f"/payments/{payment_id}/refunds", {"amount_cents": planned.refund_cents}, key)
        outcome, refund = refund_outcome(response)
        self.stats.add(f"refunds.{outcome}")
        if outcome == "error":
            self.stats.note(f"refund of payment {planned.n}: an answer the generator did not expect")
        if outcome == "unknown" and refund and isinstance(refund.get("id"), str) and not self._closing.is_set():
            self._follow_up(STATUS_CHECKS[0], self._check_status, payment_id, 0, refund["id"])

    def _check_status(self, payment_id: str, nth: int, refund_id: str | None = None) -> None:
        """Did a payment whose outcome was unknown go through after all? Or, given refund_id, one of its refunds?"""
        what = "unknown_payments" if refund_id is None else "unknown_refunds"
        try:
            response = self.api.get(f"/payments/{payment_id}")
        except httpx.HTTPError:
            self.stats.add(f"{what}.no_answer")
            return
        found = _json(response) if response.status_code == 200 else {}
        if refund_id is not None:  # a payment's status lists its refunds, each with its own status
            refunds = found.get("refunds")
            mine = (
                [r for r in refunds if isinstance(r, dict) and r.get("id") == refund_id]
                if isinstance(refunds, list)
                else []
            )
            found = mine[0] if mine else {}
        status = found.get("status")
        if status == "succeeded":
            self.stats.add(f"{what}.confirmed_later")  # by its webhook, through the payment confirmer
        elif status == "pending" and nth + 1 < len(STATUS_CHECKS) and not self._closing.is_set():
            later = STATUS_CHECKS[nth + 1] - STATUS_CHECKS[nth]
            self._follow_up(later, self._check_status, payment_id, nth + 1, refund_id)
        elif status == "pending":
            self.stats.add(f"{what}.still_pending")
        else:
            self.stats.add(f"{what}.other")


def _counts(counts: dict[str, int]) -> Callable[[str], int]:
    return lambda name: counts.get(name, 0)


def progress(counts: dict[str, int], elapsed: float, in_flight: int) -> str:
    n = _counts(counts)
    unknown = n("payments.unknown")
    return (
        f"{elapsed:4.0f} s  payments {n('payments.succeeded')} succeeded, {unknown} unknown;"
        f" refunds {n('refunds.succeeded')}; retries {n('retries')}; shed {n('shed')}; in flight {in_flight}"
    )


def _retry_reasons(counts: dict[str, int]) -> str:
    reasons = {k.removeprefix("retries.after_"): v for k, v in counts.items() if k.startswith("retries.")}
    return (
        ", ".join(f"after {'no answer' if k == 'no_answer' else k}: {v}" for k, v in sorted(reasons.items())) or "none"
    )


def summary(counts: dict[str, int]) -> list[str]:
    """The final counts, in sentences. counts: Stats.to_dict()["counts"]."""
    n = _counts(counts)
    payments = sum(n(f"payments.{o}") for o in ("succeeded", "failed", "unknown", "error"))
    refunds = sum(n(f"refunds.{o}") for o in ("succeeded", "refused", "unknown", "error"))
    return [
        f"  payments  {payments} answered: {n('payments.succeeded')} succeeded, {n('payments.failed')} refused,"
        f" {n('payments.unknown')} outcome unknown ({n('unknown_payments.confirmed_later')} confirmed later,"
        f" {n('unknown_payments.still_pending')} still pending), {n('payments.error')} unexpected",
        f"  refunds   {refunds} answered: {n('refunds.succeeded')} succeeded, {n('refunds.refused')} refused,"
        f" {n('refunds.unknown')} outcome unknown ({n('unknown_refunds.confirmed_later')} confirmed later,"
        f" {n('unknown_refunds.still_pending')} still pending), {n('refunds.error')} unexpected",
        f'  double clicks {n("double_clicks.sent")}: {n("double_clicks.in_progress")} told "still running",'
        f" {n('double_clicks.replayed')} given the saved answer, {n('double_clicks.ran_first')} ran first",
        f"  retries {n('retries')} ({_retry_reasons(counts)}), shed {n('shed')}, generator errors {n('generator_errors')};"
        f" not sent because the run ended: {n('not_sent.refunds')} refunds, {n('not_sent.status_checks')} status checks,"
        f" {n('not_sent.double_clicks')} double clicks",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m klaxon.traffic", description=__doc__.splitlines()[0])
    parser.add_argument("--rate", type=float, default=2.0, help="new payments per second, on average (default 2)")
    parser.add_argument("--seconds", type=float, default=None, help="how long to send (default: until Ctrl-C)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--lose-ack-rate", type=float, default=0.05, help="share of webhook acknowledgements lost")
    parser.add_argument(
        "--lost-answer-rate", type=float, default=0.02, help="share of charges and refunds whose answer is lost"
    )
    parser.add_argument("--double-click-share", type=float, default=0.02)
    parser.add_argument("--refund-share", type=float, default=0.10)
    parser.add_argument("--max-in-flight", type=int, default=64)
    parser.add_argument("--json", action="store_true", help="print the final counts as JSON")
    args = parser.parse_args(argv)
    try:
        settings = Settings(
            rate=args.rate,
            seconds=args.seconds,
            seed=args.seed,
            lose_ack_rate=args.lose_ack_rate,
            lost_answer_rate=args.lost_answer_rate,
            double_click_share=args.double_click_share,
            refund_share=args.refund_share,
            max_in_flight=args.max_in_flight,
        )
    except ValueError as exc:
        parser.error(str(exc))
    generator = TrafficGenerator(settings)
    try:
        generator.start()
    except httpx.HTTPError as exc:
        print(f"Could not reach the fake provider (is the stack up? make up): {exc}", file=sys.stderr)
        return 2
    started = time.monotonic()
    print(
        f"Synthetic traffic (run {settings.run_id}, seed {settings.seed}): {settings.rate:g} payments per second"
        f" {'for ' + format(settings.seconds, 'g') + ' s' if settings.seconds else 'until Ctrl-C'}."
        f" Provider noise: lose_ack_rate {settings.lose_ack_rate:g}, lost_answer_rate {settings.lost_answer_rate:g}."
        " Synthetic data, fake provider, this Mac only.",
        flush=True,
    )
    try:
        while not generator.wait(10.0):
            print(
                f"{datetime.now():%H:%M:%S}  "
                + progress(generator.stats.to_dict()["counts"], time.monotonic() - started, generator.in_flight),
                flush=True,
            )
    except KeyboardInterrupt:
        print("\nStopping: nothing new is sent, and retries are given up.", flush=True)
        generator.stop_now()
        generator.join()
    elapsed = time.monotonic() - started
    if args.json:
        print(json.dumps({"settings": asdict(settings), "seconds": round(elapsed, 1)} | generator.stats.to_dict()))
    else:
        print(
            f"Done after {elapsed:.0f} s.",
            *summary(generator.stats.to_dict()["counts"]),
            f"  {generator.behavior_note}",
            sep="\n",
        )
        for what in generator.stats.unexpected:
            print(f"  unexpected: {what}")
    return 1 if generator.stats["generator_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
