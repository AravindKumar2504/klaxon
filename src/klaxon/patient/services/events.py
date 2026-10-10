"""Provider event payloads: one parser for the webhook receiver and the ledger consumer.

The shape is the first piece's (`klaxon.patient.provider.Event.payload`):

    {"id": "evt_000001", "type": "payment.succeeded", "created": 1791504000,
     "data": {"payment_id": "pay_000001", "amount_cents": 1999, "currency": "usd"}}

The receiver only needs the event id and a partition key, so it uses
`id_and_key`. The consumer books money, so it uses `parse_event`, which checks
every field the ledger depends on. Anything either refuses raises BadEvent:
the receiver answers 400, the consumer dead-letters the message. Ids must be
short plain ASCII, so no value that reaches the database can be refused there
(a lone surrogate cannot be encoded, a 3 KB id does not fit in an index row).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

CURRENCY = "usd"  # the ledger is single-currency, like the first piece
# $999,999.99, Stripe's limit for one USD charge. Anything bigger is not plausible here: hold it for a human.
MAX_AMOUNT_CENTS = 99_999_999
MAX_EVENT_BYTES = 64 * 1024  # the receiver refuses bigger bodies too
ID = re.compile(r"[A-Za-z0-9_]{1,64}")  # evt_000001, pay_000001
TYPE = re.compile(r"[a-z][a-z_.]{0,63}")  # payment.succeeded


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


def _is_id(value: object) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _load(body: object) -> dict:
    if not isinstance(body, (bytes, bytearray)):
        raise BadEvent("no message body")  # a Kafka message with a null value, for example
    if len(body) > MAX_EVENT_BYTES:
        raise BadEvent(f"body of {len(body)} bytes is above the {MAX_EVENT_BYTES} byte limit")
    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadEvent(f"not JSON: {exc}") from None
    except RecursionError:
        raise BadEvent("JSON nested too deeply") from None
    if not isinstance(event, dict):
        raise BadEvent("not a JSON object")
    if not _is_id(event.get("id")):
        raise BadEvent("missing or malformed event id")
    return event


def id_and_key(body: bytes) -> tuple[str, str]:
    """The event id, and the Kafka key: the payment id, so one payment's events share a partition."""
    event = _load(body)
    data = event.get("data")
    payment_id = data.get("payment_id") if isinstance(data, dict) else None
    return event["id"], payment_id if _is_id(payment_id) else event["id"]


def parse_event(body: bytes) -> ProviderEvent:
    event = _load(body)
    data = event.get("data")
    if not isinstance(event.get("type"), str) or TYPE.fullmatch(event["type"]) is None:
        raise BadEvent("missing or malformed event type")
    if not isinstance(event.get("created"), int) or isinstance(event["created"], bool):
        raise BadEvent("created must be unix seconds")
    if not isinstance(data, dict):
        raise BadEvent("missing data")
    if not _is_id(data.get("payment_id")):
        raise BadEvent("missing or malformed payment id")
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
