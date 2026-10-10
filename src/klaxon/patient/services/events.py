"""Provider event payloads: one parser for the webhook receiver and the ledger consumer.

The shape is the first piece's (`klaxon.patient.provider.Event.payload`):

    {"id": "evt_000001", "type": "payment.succeeded", "created": 1791504000,
     "data": {"payment_id": "pay_000001", "amount_cents": 1999, "currency": "usd"}}

The receiver only needs the event id and a partition key, so it uses
`id_and_key`. The consumer books money, so it uses `parse_event`, which checks
every field the ledger depends on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

CURRENCY = "usd"  # the ledger is single-currency, like the first piece
# $999,999.99, Stripe's limit for one USD charge. Anything bigger is not plausible here: hold it for a human.
MAX_AMOUNT_CENTS = 99_999_999


class BadEvent(ValueError):
    """The payload cannot be booked as it is. It goes to the dead-letter topic, never silently away."""


@dataclass(frozen=True)
class ProviderEvent:
    id: str
    type: str
    created: int
    payment_id: str
    amount_cents: int
    currency: str


def _is_cents(value: object) -> bool:
    # bool is a subclass of int; True is not one cent.
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _load(body: bytes) -> dict:
    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadEvent(f"not JSON: {exc}") from None
    if not isinstance(event, dict):
        raise BadEvent("not a JSON object")
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise BadEvent("missing event id")
    return event


def id_and_key(body: bytes) -> tuple[str, str]:
    """The event id, and the Kafka key: the payment id, so one payment's events share a partition."""
    event = _load(body)
    data = event.get("data")
    payment_id = data.get("payment_id") if isinstance(data, dict) else None
    return event["id"], payment_id if isinstance(payment_id, str) and payment_id else event["id"]


def parse_event(body: bytes) -> ProviderEvent:
    event = _load(body)
    data = event.get("data")
    if not isinstance(event.get("type"), str) or not event["type"]:
        raise BadEvent("missing event type")
    if not isinstance(event.get("created"), int) or isinstance(event["created"], bool):
        raise BadEvent("created must be unix seconds")
    if not isinstance(data, dict):
        raise BadEvent("missing data")
    if not isinstance(data.get("payment_id"), str) or not data["payment_id"]:
        raise BadEvent("missing payment id")
    if not _is_cents(data.get("amount_cents")):
        raise BadEvent("amount must be positive integer cents")
    if data["amount_cents"] > MAX_AMOUNT_CENTS:
        raise BadEvent(f"amount {data['amount_cents']} cents is above the {MAX_AMOUNT_CENTS} cents limit")
    if data.get("currency") != CURRENCY:
        raise BadEvent(f"unsupported currency {data.get('currency')!r}")
    return ProviderEvent(
        id=event["id"],
        type=event["type"],
        created=event["created"],
        payment_id=data["payment_id"],
        amount_cents=data["amount_cents"],
        currency=data["currency"],
    )
