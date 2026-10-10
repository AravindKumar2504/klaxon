"""The ledger consumer's message handling: book first, commit second, never skip a money event."""

import json
import threading

import psycopg
import pytest
from confluent_kafka import KafkaException

from klaxon.patient.services.common import Heartbeat
from klaxon.patient.services.kafka_io import DLQ_TOPIC
from klaxon.patient.services.ledger_consumer import LedgerConsumer
from tests.services.fakes import FakeConsumer, FakeMessage, FakePublisher


def event(n: int = 1, amount: int = 1999) -> bytes:
    return json.dumps(
        {
            "id": f"evt_{n:06d}",
            "type": "payment.succeeded",
            "created": 1_791_504_000,
            "data": {"payment_id": f"pay_{n:06d}", "amount_cents": amount, "currency": "usd"},
        }
    ).encode()


class Booker:
    """Books into a list. errors: exceptions to raise, one per call, before it starts succeeding."""

    def __init__(self, log: list[str], errors: list[Exception] | None = None) -> None:
        self.log = log
        self.errors = list(errors or [])
        self.booked: list[tuple[str, str]] = []

    def book(self, event, source):
        if self.errors:
            raise self.errors.pop(0)
        self.log.append(f"book {event.id}")
        self.booked.append((event.id, source))
        return "posted"


def consumer_for(messages, errors=None, dead_letters=None, heartbeat=None):
    stop = threading.Event()
    log: list[str] = []
    booker = Booker(log, errors)
    kafka = FakeConsumer(messages, stop, log)
    dead_letters = dead_letters or FakePublisher()
    worker = LedgerConsumer(kafka, booker, dead_letters, heartbeat=heartbeat, retry_delays=(0.0,))
    return worker, kafka, booker, dead_letters, stop, log


def test_each_message_is_booked_and_only_then_committed():
    worker, kafka, booker, _, stop, log = consumer_for([FakeMessage(event(1), 5), FakeMessage(event(2), 6, 2)])
    worker.run(stop)
    assert log == ["book evt_000001", "commit 5", "book evt_000002", "commit 6"]
    assert booker.booked == [("evt_000001", "provider-events/0@5"), ("evt_000002", "provider-events/2@6")]


def test_an_unbookable_message_goes_to_the_dead_letter_topic_with_its_reason_then_is_committed():
    worker, kafka, booker, dead_letters, stop, _ = consumer_for([FakeMessage(b"{not json", 3)])
    worker.run(stop)
    [letter] = dead_letters.messages
    assert letter["topic"] == DLQ_TOPIC and letter["value"] == b"{not json"
    assert letter["headers"]["source"] == "provider-events/0@3"
    assert letter["headers"]["reason"].startswith("not bookable")
    assert booker.booked == [] and kafka.committed == [3]


def test_a_database_outage_retries_the_same_message_and_never_commits_it_early():
    outage = [psycopg.OperationalError("connection refused")] * 3
    worker, kafka, booker, _, stop, log = consumer_for([FakeMessage(event(1), 9)], errors=outage)
    worker.run(stop)
    assert log == ["book evt_000001", "commit 9"]  # three failures, then one booking, then one commit


def test_permissions_or_schema_problems_are_retried_too_not_dead_lettered():
    errors = [psycopg.errors.InsufficientPrivilege("permission denied for table entries")]
    worker, kafka, booker, dead_letters, stop, _ = consumer_for([FakeMessage(event(1), 1)], errors=errors)
    worker.run(stop)
    assert dead_letters.messages == [] and kafka.committed == [1]


def test_a_value_the_database_refuses_is_dead_lettered():
    errors = [psycopg.errors.NumericValueOutOfRange("bigint out of range")]
    worker, kafka, booker, dead_letters, stop, _ = consumer_for([FakeMessage(event(1), 4)], errors=errors)
    worker.run(stop)
    assert dead_letters.messages[0]["headers"]["reason"].startswith("database refused the values")
    assert kafka.committed == [4]


def test_shutting_down_while_retrying_leaves_the_message_uncommitted():
    worker, kafka, booker, _, stop, _ = consumer_for([], errors=[psycopg.OperationalError("down")])
    stop.set()  # the stop arrives while the booking is failing
    assert worker.handle(FakeMessage(event(1), 7), stop) is False
    assert kafka.committed == [] and booker.booked == []


def test_if_the_dead_letter_topic_is_down_the_message_is_not_committed_either():
    worker, kafka, _, _, stop, _ = consumer_for([], dead_letters=FakePublisher(fail=RuntimeError("kafka down")))
    stop.set()
    assert worker.handle(FakeMessage(b"garbage", 2), stop) is False
    assert kafka.committed == []


def test_a_failed_commit_does_not_stop_the_loop():
    # The uncommitted message will be read again later; booking it twice is a no-op (tests/integration).
    worker, kafka, booker, _, stop, _ = consumer_for([FakeMessage(event(1), 1), FakeMessage(event(2), 2)])
    kafka.commit_error = KafkaException("rebalance in progress")
    worker.run(stop)  # does not raise
    assert [e for e, _ in booker.booked] == ["evt_000001", "evt_000002"]


def test_kafka_errors_are_skipped_without_booking_or_committing():
    worker, kafka, booker, _, stop, _ = consumer_for([FakeMessage(b"", 0, error="broker transport failure")])
    worker.run(stop)
    assert booker.booked == [] and kafka.committed == []


def test_the_heartbeat_file_shows_the_loop_is_alive(tmp_path):
    beat = tmp_path / "heartbeat"
    worker, *_, stop, _ = consumer_for([FakeMessage(event(1), 1)], heartbeat=Heartbeat(str(beat)))
    worker.run(stop)
    assert beat.exists()


@pytest.mark.parametrize("value", [event(1, amount=0), event(1, amount=10**12)])
def test_amounts_the_ledger_must_not_book_are_held_for_a_human(value):
    worker, kafka, booker, dead_letters, stop, _ = consumer_for([FakeMessage(value, 1)])
    worker.run(stop)
    assert booker.booked == [] and len(dead_letters.messages) == 1


@pytest.mark.parametrize("value", [None, b"[" * 50_000, json.dumps({"id": "\ud800"}).encode()])
def test_odd_messages_are_dead_lettered_instead_of_crashing_the_consumer(value):
    worker, kafka, booker, dead_letters, stop, _ = consumer_for([FakeMessage(value, 1), FakeMessage(event(2), 2)])
    worker.run(stop)  # does not raise
    assert dead_letters.messages[0]["headers"]["reason"].startswith("not bookable")
    assert [e for e, _ in booker.booked] == ["evt_000002"]  # the partition keeps moving
    assert kafka.committed == [1, 2]


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.CheckViolation("new row violates check constraint"),
        psycopg.errors.ForeignKeyViolation("violates foreign key constraint"),
        psycopg.errors.ProgramLimitExceeded("index row size exceeds btree maximum"),
        UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"),
    ],
)
def test_values_the_database_refuses_are_dead_lettered_not_retried_forever(error):
    worker, kafka, booker, dead_letters, stop, _ = consumer_for([FakeMessage(event(1), 4)], errors=[error])
    worker.run(stop)
    assert dead_letters.messages[0]["headers"]["reason"].startswith("database refused the values")
    assert kafka.committed == [4]
