"""The payment confirmer: marks our payments succeeded from the provider's webhooks.

Our payments API learns a payment's outcome from the provider's answer. When
no answer it can trust arrives (timeouts, or a 500 after the provider already
saved the charge), it cannot know whether the customer was charged. It leaves
the payment pending and tells the client to retry with the same key. If the
client never retries, the payment stays pending forever, and a pending
payment cannot be refunded.

The provider's webhook settles it. Every payment.succeeded event carries the
metadata the API sent with the charge, which holds our own payment id. This
consumer reads those events in its own consumer group, "payment-confirmer",
apart from the ledger, and moves the payment from pending to succeeded in one
statement that also checks the amount:

    UPDATE payments SET status = 'succeeded', provider_payment_id = <the provider's id>
     WHERE id = <our id, from the metadata> AND status = 'pending' AND amount_cents = <the event's amount>
       AND (provider_payment_id IS NULL OR provider_payment_id = <the provider's id>)

A status only moves forward from pending: a final one (succeeded, failed) is
never overwritten, and an amount is never changed (the confirmer's database
role cannot change one). The loop around it is the ledger's (see `consumer`):
apply, then commit the offset. A repeated event changes nothing.

Outcomes, one per event:
- confirmed: a pending payment moved to succeeded.
- already_confirmed: the API's own answer, or an earlier copy of this event,
  got there first. The usual case.
- conflict: the event disagrees with our records: we said failed, the
  amounts differ, the payment is linked to another provider payment, or
  another payment of ours is already linked to this one. Nothing changes. It
  is logged as an error for a human, and the money check's fourth invariant
  reports it. It is not dead-lettered: the message is fine, our records
  disagree with it, and retrying cannot change that.
- unknown_payment: no payment of ours matches the event. Logged as an error.
- not_found: the known-bad mode's reading of the same thing (see below).
- ignored_type: not a payment.succeeded event.

Modes:
- "by_our_id" (default): find the payment by our own id, from the metadata.
- "by_provider_id": the known-bad baseline. Find it by the provider's payment
  id, the way many integrations do. That only works once the API has heard
  the provider's answer, which is exactly when no confirmation is needed. A
  payment whose answer was lost never got the provider's id, so it is not
  found, nothing looks wrong, and it stays pending. Only the money check
  notices.

Run: python -m klaxon.patient.services.payment_confirmer
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from klaxon.patient.provider import PAYMENT_SUCCEEDED
from klaxon.patient.services import telemetry
from klaxon.patient.services.common import configure_logging, env_str, log
from klaxon.patient.services.consumer import open_pool, serve
from klaxon.patient.services.events import ProviderEvent
from klaxon.patient.services.kafka_io import CONFIRMER_GROUP

logger = logging.getLogger("klaxon.payment_confirmer")

MODES = ("by_our_id", "by_provider_id")

COLUMNS = "id, status, amount_cents, provider_payment_id"


@dataclass(frozen=True)
class OurPayment:
    id: str
    status: str
    amount_cents: int
    provider_payment_id: str | None


def why_unchanged(event: ProviderEvent, ours: OurPayment | None, mode: str) -> tuple[str, str | None]:
    """The confirmation changed nothing: why? Returns (outcome, problem); problem is None when all is well."""
    if ours is None:
        if mode == "by_provider_id":
            return "not_found", None  # the naive reading: "not one of ours, nothing to do"
        if event.klaxon_payment_id is None:
            return "unknown_payment", "the provider charged a payment whose metadata names no payment of ours"
        return "unknown_payment", f"the provider charged {event.klaxon_payment_id}, which is not in our records"
    if ours.status == "failed":
        return "conflict", "the provider charged a payment we recorded as failed"
    if ours.amount_cents != event.amount_cents:
        return "conflict", "the provider charged a different amount than our payment's"
    if ours.provider_payment_id not in (None, event.payment_id):
        return "conflict", f"our payment is linked to another provider payment ({ours.provider_payment_id})"
    if ours.status == "succeeded":
        return "already_confirmed", None
    return "conflict", f"the payment is {ours.status} and could not be confirmed"  # a race; not seen so far


class PgConfirmer:
    def __init__(self, pool: ConnectionPool, mode: str = "by_our_id") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        self.pool = pool  # connections in autocommit mode; transaction() blocks make the transactions
        self.mode = mode

    def confirm(self, event: ProviderEvent, source: str) -> str:
        """Confirm one provider event. Returns the outcome (see the module docstring)."""
        if event.type != PAYMENT_SUCCEEDED:
            return "ignored_type"
        try:
            changed, ours = self._update(event)
        except psycopg.errors.UniqueViolation:  # the provider's id is already on another payment of ours
            outcome, problem, ours = (
                "conflict",
                f"another payment of ours is already linked to {event.payment_id}",
                None,
            )
        else:
            if changed:
                return "confirmed"
            outcome, problem = why_unchanged(event, ours, self.mode)
        if problem is not None:
            log(
                logger,
                logging.ERROR,
                f"not confirmed: {problem}",
                event_id=event.id,
                provider_payment_id=event.payment_id,
                provider_amount_cents=event.amount_cents,
                klaxon_payment_id=event.klaxon_payment_id,
                our_status=ours.status if ours else None,
                our_amount_cents=ours.amount_cents if ours else None,
                our_provider_payment_id=ours.provider_payment_id if ours else None,
                source=source,
            )
        return outcome

    def _update(self, event: ProviderEvent) -> tuple[bool, OurPayment | None]:
        """Try the confirmation, in one transaction. When it changed nothing, also read the payment, to say why."""
        if self.mode == "by_our_id" and event.klaxon_payment_id is None:
            return False, None
        with self.pool.connection() as conn, conn.transaction():
            if self.mode == "by_our_id":
                changed = conn.execute(
                    "UPDATE payments SET status = 'succeeded', provider_payment_id = %(provider)s, updated_at = now()"
                    " WHERE id = %(ours)s AND status = 'pending' AND amount_cents = %(amount)s"
                    "   AND (provider_payment_id IS NULL OR provider_payment_id = %(provider)s)",
                    {"provider": event.payment_id, "ours": event.klaxon_payment_id, "amount": event.amount_cents},
                ).rowcount
                lookup = f"SELECT {COLUMNS} FROM payments WHERE id = %s", (event.klaxon_payment_id,)
            else:
                changed = conn.execute(
                    "UPDATE payments SET status = 'succeeded', updated_at = now()"
                    " WHERE provider_payment_id = %s AND status = 'pending' AND amount_cents = %s",
                    (event.payment_id, event.amount_cents),
                ).rowcount
                lookup = f"SELECT {COLUMNS} FROM payments WHERE provider_payment_id = %s", (event.payment_id,)
            if changed:
                return True, None
            row = conn.execute(*lookup).fetchone()
        return False, OurPayment(*row) if row is not None else None


def main() -> None:
    configure_logging("payment-confirmer")
    telemetry.setup("payment-confirmer")  # before the pool, so its connections are traced
    mode = env_str("CONFIRMER_HANDLER", "by_our_id")
    pool = open_pool("confirmer")
    log(logger, logging.INFO, "payment confirmer starting", mode=mode)
    try:
        serve(
            PgConfirmer(pool, mode).confirm,
            group=CONFIRMER_GROUP,
            verb="confirm",
            heartbeat_path="/tmp/payment-confirmer.heartbeat",
        )
    finally:
        pool.close()
        log(logger, logging.INFO, "payment confirmer stopped")


if __name__ == "__main__":
    main()
