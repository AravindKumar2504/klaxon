"""A fake, Stripe-like payment provider (synthetic, in-process, no real money).

It creates payments and refunds and keeps its own records. Those records are
the source of truth the money check reconciles our ledger against.

It delivers each event to our webhook endpoint as an HMAC-signed request, on
one of three schedules:

- "normal": every event once, in the order it happened.
- "duplicate": every event once, and one of them a second time. Webhook
  providers deliver at least once: if our 200 does not reach them in time,
  they retry. Here the acknowledgement of one delivery is treated as lost.
- "out_of_order": every event once, in a seeded shuffled order. Providers do
  not promise order, so a refund can arrive before its payment.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass

from klaxon.patient import signatures

SCHEDULES = ("normal", "duplicate", "out_of_order")

PAYMENT_SUCCEEDED = "payment.succeeded"
REFUND_SUCCEEDED = "refund.succeeded"

# 2026-10-09 00:00:00 UTC. A simulated clock, so runs are reproducible.
SIMULATED_START = 1_791_504_000


class FakeClock:
    """Unix seconds that move only when told to."""

    def __init__(self, start: int = SIMULATED_START) -> None:
        self._now = start

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int = 1) -> int:
        self._now += seconds
        return self._now


@dataclass(frozen=True)
class Payment:
    id: str
    customer: str
    amount_cents: int


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    payment_id: str
    amount_cents: int
    created: int

    def payload(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "created": self.created,
            "data": {"payment_id": self.payment_id, "amount_cents": self.amount_cents, "currency": "usd"},
        }


@dataclass(frozen=True)
class WebhookRequest:
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class PlannedDelivery:
    event: Event
    note: str = ""


@dataclass(frozen=True)
class Delivery:
    n: int  # position in the delivery sequence, starting at 1
    event_id: str
    status: int  # what our endpoint answered
    note: str = ""


def _require_cents(amount_cents: int) -> None:
    # bool is a subclass of int; True is not one cent.
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise ValueError(f"amount must be a positive whole number of cents, got {amount_cents!r}")


class FakeProvider:
    def __init__(self, secret: bytes, clock: FakeClock | None = None, seed: int = 0) -> None:
        self.secret = secret
        self.clock = clock or FakeClock()
        self._rng = random.Random(seed)
        self.payments: dict[str, Payment] = {}
        self.events: list[Event] = []
        self._refunded_cents: dict[str, int] = {}

    def create_payment(self, customer: str, amount_cents: int) -> Payment:
        _require_cents(amount_cents)
        payment = Payment(f"pay_{len(self.payments) + 1:04d}", customer, amount_cents)
        self.payments[payment.id] = payment
        self._record(PAYMENT_SUCCEEDED, payment.id, amount_cents)
        return payment

    def refund(self, payment_id: str, amount_cents: int) -> Event:
        _require_cents(amount_cents)
        payment = self.payments[payment_id]
        already = self._refunded_cents.get(payment_id, 0)
        if already + amount_cents > payment.amount_cents:
            raise ValueError(f"refund of {amount_cents} would exceed what {payment_id} captured")
        self._refunded_cents[payment_id] = already + amount_cents
        return self._record(REFUND_SUCCEEDED, payment_id, amount_cents)

    def balance_cents(self) -> int:
        """What the provider says it holds for us: captured minus refunded."""
        captured = sum(p.amount_cents for p in self.payments.values())
        return captured - sum(self._refunded_cents.values())

    def event(self, event_id: str) -> Event:
        found = next((e for e in self.events if e.id == event_id), None)
        if found is None:
            raise KeyError(event_id)
        return found

    def signed_request(self, event: Event) -> WebhookRequest:
        body = json.dumps(event.payload(), separators=(",", ":"), sort_keys=True).encode()
        header = signatures.sign(self.secret, self.clock.now(), body)
        return WebhookRequest(body=body, headers={signatures.HEADER: header, "Content-Type": "application/json"})

    def plan(self, schedule: str, duplicate_event_id: str | None = None) -> list[PlannedDelivery]:
        """The order deliveries will be sent in. Seeded, so the same seed gives the same plan."""
        if schedule == "normal":
            return [PlannedDelivery(e) for e in self.events]
        if schedule == "duplicate":
            return self._duplicate_plan(duplicate_event_id)
        if schedule == "out_of_order":
            shuffled = list(self.events)
            self._rng.shuffle(shuffled)
            if shuffled == self.events:
                shuffled.reverse()
            return [PlannedDelivery(e) for e in shuffled]
        raise ValueError(f"unknown schedule {schedule!r}; expected one of {SCHEDULES}")

    def deliver(self, endpoint: Callable[[WebhookRequest], int], plan: list[PlannedDelivery]) -> list[Delivery]:
        deliveries = []
        for n, planned in enumerate(plan, start=1):
            self.clock.advance(1)
            status = endpoint(self.signed_request(planned.event))
            deliveries.append(Delivery(n, planned.event.id, status, planned.note))
        return deliveries

    def _duplicate_plan(self, duplicate_event_id: str | None) -> list[PlannedDelivery]:
        if duplicate_event_id is None:
            payment_events = [e.id for e in self.events if e.type == PAYMENT_SUCCEEDED]
            if not payment_events:
                raise ValueError("the duplicate schedule needs at least one payment")
            duplicate_event_id = self._rng.choice(payment_events)
        position = next((i for i, e in enumerate(self.events, start=1) if e.id == duplicate_event_id), None)
        if position is None:
            raise ValueError(f"no event {duplicate_event_id!r} to duplicate")

        plan = []
        for e in self.events:
            lost = e.id == duplicate_event_id
            plan.append(PlannedDelivery(e, "acknowledgement lost (simulated timeout)" if lost else ""))
        # The provider retries later, after the deliveries already queued.
        plan.append(PlannedDelivery(self.event(duplicate_event_id), f"retry of #{position}"))
        return plan

    def _record(self, event_type: str, payment_id: str, amount_cents: int) -> Event:
        self.clock.advance(1)
        event = Event(f"evt_{len(self.events) + 1:04d}", event_type, payment_id, amount_cents, self.clock.now())
        self.events.append(event)
        return event
