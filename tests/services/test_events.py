import json

import pytest

from klaxon.patient.provider import Event
from klaxon.patient.services.events import MAX_AMOUNT_CENTS, BadEvent, ProviderEvent, id_and_key, parse_event


def body(**changes) -> bytes:
    event = {
        "id": "evt_000001",
        "type": "payment.succeeded",
        "created": 1_791_504_001,
        "data": {"payment_id": "pay_000001", "amount_cents": 1999, "currency": "usd"},
    }
    data = changes.pop("data", {})
    event.update(changes)
    event["data"] = event["data"] | data
    return json.dumps(event).encode()


def test_parses_the_first_pieces_payload_shape():
    first_piece = json.dumps(Event("evt_0001", "payment.succeeded", "pay_0001", 2_500, 7).payload()).encode()
    assert parse_event(first_piece) == ProviderEvent("evt_0001", "payment.succeeded", 7, "pay_0001", 2_500, "usd")


def test_the_kafka_key_is_the_payment_id_so_a_payments_events_share_a_partition():
    assert id_and_key(body()) == ("evt_000001", "pay_000001")
    assert id_and_key(json.dumps({"id": "evt_9"}).encode()) == ("evt_9", "evt_9")  # no payment id: key by event


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"\xff\xfe",
        b"[1, 2]",
        json.dumps({"type": "payment.succeeded"}).encode(),
        json.dumps({"id": ""}).encode(),
        json.dumps({"id": 42}).encode(),
    ],
)
def test_the_receiver_refuses_what_has_no_event_id(raw):
    with pytest.raises(BadEvent):
        id_and_key(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": ""},
        {"type": None},
        {"created": True},
        {"created": "yesterday"},
        {"data": {"payment_id": ""}},
        {"data": {"payment_id": 7}},
        {"data": {"amount_cents": 0}},
        {"data": {"amount_cents": -5}},
        {"data": {"amount_cents": 19.99}},
        {"data": {"amount_cents": True}},
        {"data": {"amount_cents": "1999"}},
        {"data": {"amount_cents": MAX_AMOUNT_CENTS + 1}},
        {"data": {"currency": "eur"}},
        {"data": {"currency": None}},
    ],
)
def test_the_consumer_refuses_anything_it_cannot_book_safely(changes):
    with pytest.raises(BadEvent):
        parse_event(body(**changes))


def test_missing_data_is_refused():
    with pytest.raises(BadEvent):
        parse_event(json.dumps({"id": "evt_1", "type": "payment.succeeded", "created": 1}).encode())


def test_the_largest_plausible_amount_is_accepted():
    assert parse_event(body(data={"amount_cents": MAX_AMOUNT_CENTS})).amount_cents == MAX_AMOUNT_CENTS


@pytest.mark.parametrize(
    "raw",
    [
        None,  # a Kafka message with a null value (a "tombstone")
        b"[" * 50_000,  # under the size limit, but nested so deeply that the JSON parser gives up
        b"[" * 70_000,  # over the size limit
        b'{"id": "' + b"x" * (64 * 1024) + b'"}',  # bigger than any event the receiver accepts
        json.dumps({"id": "\ud800"}).encode(),  # a lone surrogate: PostgreSQL could not even encode it
        json.dumps({"id": "e" * 65}).encode(),  # too long for an id (a 3 KB id breaks the index)
        json.dumps({"id": "évt_1"}).encode(),  # not plain ASCII
        json.dumps({"id": "evt 1"}).encode(),
    ],
)
def test_odd_bodies_are_refused_as_bad_events_not_crashes(raw):
    with pytest.raises(BadEvent):
        id_and_key(raw)
    with pytest.raises(BadEvent):
        parse_event(raw)


@pytest.mark.parametrize("payment_id", ["\ud800", "p" * 65, "pay 1", "pay_1;DROP"])
def test_malformed_payment_ids_are_refused(payment_id):
    with pytest.raises(BadEvent):
        parse_event(body(data={"payment_id": payment_id}))
    assert id_and_key(body(data={"payment_id": payment_id})) == ("evt_000001", "evt_000001")  # keyed by event


@pytest.mark.parametrize("event_type", ["Payment.Succeeded", "payment succeeded", "x" * 65, "1payment"])
def test_malformed_event_types_are_refused(event_type):
    with pytest.raises(BadEvent):
        parse_event(body(type=event_type))
