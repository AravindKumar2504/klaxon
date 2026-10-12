"""The fake provider's webhook sender: at least once, signed, retried with backoff.

Like a real provider, it keeps retrying an event until our endpoint answers
2xx, for up to three days (Stripe's window), so an outage on our side shorter
than that loses nothing. The price is duplicates: if an acknowledgement gets
lost, the provider cannot tell "never arrived" from "arrived, answer lost", so
it sends the event again. Our side must ignore the repeat.

Simulator controls (a real provider has none of these). They live in Behavior,
which the provider's HTTP API reads too:
- lose_ack_rate: the share of 2xx answers the provider pretends it never got.
  Seeded per (event, attempt), so the same seed loses the same acknowledgements.
- lose_ack_event_ids: lose the acknowledgement of these events' first delivery,
  the way the first piece's demo lost delivery #3's.
- fail_after_charge_rate: the share of payment requests the provider charges
  and then answers with a 500, as if it crashed after saving the charge.
  Seeded per idempotency key, and the same key always gets the same 500, the
  way Stripe replays the first answer for a key, errors included. Our side
  cannot tell whether the customer was charged; only the webhook can say.
- fail_after_charge_keys: do that for these idempotency keys.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from klaxon.patient import signatures
from klaxon.patient.services.common import log
from klaxon.patient.services.provider_store import DueDelivery, ProviderStore

logger = logging.getLogger("klaxon.provider.delivery")

BASE_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 60.0
MAX_AGE_SECONDS = 3 * 24 * 3600  # Stripe keeps retrying for up to three days


@dataclass(frozen=True)
class Behavior:
    lose_ack_rate: float = 0.0
    seed: int = 0
    lose_ack_event_ids: frozenset[str] = field(default_factory=frozenset)
    fail_after_charge_rate: float = 0.0
    fail_after_charge_keys: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in ("lose_ack_rate", "fail_after_charge_rate"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")

    def ack_lost(self, event_id: str, attempt: int) -> bool:
        if attempt == 1 and event_id in self.lose_ack_event_ids:
            return True
        if self.lose_ack_rate <= 0.0:
            return False
        return random.Random(f"{self.seed}:{event_id}:{attempt}").random() < self.lose_ack_rate

    def fails_after_charge(self, idempotency_key: str) -> bool:
        if idempotency_key in self.fail_after_charge_keys:
            return True
        if self.fail_after_charge_rate <= 0.0:
            return False
        return random.Random(f"{self.seed}:answer:{idempotency_key}").random() < self.fail_after_charge_rate

    def to_dict(self) -> dict:
        return {
            "lose_ack_rate": self.lose_ack_rate,
            "seed": self.seed,
            "lose_ack_event_ids": sorted(self.lose_ack_event_ids),
            "fail_after_charge_rate": self.fail_after_charge_rate,
            "fail_after_charge_keys": sorted(self.fail_after_charge_keys),
        }


def backoff_seconds(attempt: int) -> float:
    """0.5 s after the first failure, then doubling, capped at a minute."""
    return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * 2 ** (attempt - 1))


class WebhookSender:
    def __init__(
        self,
        store: ProviderStore,
        url: str,
        secret: bytes,
        client: httpx.Client,
        behavior: Behavior | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.url = url
        self.secret = secret
        self.client = client
        self.behavior = behavior or Behavior()
        self.clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def deliver_due(self) -> int:
        """Send every delivery that is due now, one at a time. Returns how many were attempted."""
        due = self.store.due_deliveries(self.clock())
        for delivery in due:
            self._attempt(delivery)
        return len(due)

    def _attempt(self, delivery: DueDelivery) -> None:
        attempt = delivery.attempts + 1
        sent_at = self.clock()
        headers = {
            signatures.HEADER: signatures.sign(self.secret, int(sent_at), delivery.body),
            "Content-Type": "application/json",
        }
        status: int | None = None
        error: str | None = None
        try:
            status = self.client.post(self.url, content=delivery.body, headers=headers).status_code
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"

        accepted = status is not None and 200 <= status < 300
        ack_lost = accepted and self.behavior.ack_lost(delivery.event_id, attempt)
        if ack_lost:
            error = "acknowledgement lost on purpose (simulated timeout)"
        if accepted and not ack_lost:
            outcome = "delivered"
        elif sent_at - delivery.created > MAX_AGE_SECONDS:
            outcome = "abandoned"
        else:
            outcome = "retry"
        next_attempt_at = sent_at + backoff_seconds(attempt)
        self.store.record_attempt(
            delivery.event_id, attempt, sent_at, status, error, ack_lost, outcome, next_attempt_at
        )
        log(
            logger,
            logging.INFO if outcome == "delivered" else logging.WARNING,
            f"webhook {outcome}",
            event_id=delivery.event_id,
            attempt=attempt,
            status=status,
            error=error,
            ack_lost=ack_lost,
        )

    # Background thread ---------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="webhook-sender", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """A new event exists: send it now instead of at the next poll."""
        self._wake.set()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()  # before sending, so an event created meanwhile wakes the next wait
            wait = 1.0
            try:
                self.deliver_due()
                next_due = self.store.next_due_at()
                if next_due is not None:
                    wait = min(1.0, max(0.0, next_due - self.clock()))
            except Exception:
                logger.exception("webhook sender loop failed; retrying in a second")
            self._wake.wait(wait)
