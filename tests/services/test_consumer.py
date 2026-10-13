"""The loop both consumers share: apply first, commit second, never skip a money event.

Tested here with the ledger's verb ("book"); the payment confirmer runs the same loop.
"""

import json
import threading

import psycopg
import pytest
from confluent_kafka import KafkaException
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from klaxon.patient.services.common import Heartbeat
from klaxon.patient.services.consumer import EventConsumer
from klaxon.patient.services.kafka_io import DLQ_TOPIC, LEDGER_GROUP
from klaxon.patient.services.telemetry import kafka_trace_headers
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
    worker = EventConsumer(
        kafka, booker.book, dead_letters, name=LEDGER_GROUP, verb="book", heartbeat=heartbeat, retry_delays=(0.0,)
    )
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
    assert letter["headers"]["reason"].startswith("cannot book this event")
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
    assert dead_letters.messages[0]["headers"]["reason"].startswith("cannot book this event")
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


def test_a_dead_letter_names_the_consumer_that_refused_it():
    # Both consumer groups read the same topic, so a poison message can be refused by each; the header tells them apart.
    worker, kafka, booker, dead_letters, stop, _ = consumer_for([FakeMessage(b"{not json", 3)])
    worker.run(stop)
    assert dead_letters.messages[0]["headers"]["consumer"] == LEDGER_GROUP


def test_the_logs_say_what_the_consumer_was_trying_to_do(caplog):
    stop = threading.Event()
    kafka = FakeConsumer([FakeMessage(event(1), 1)], stop)
    confirmer = Booker([], errors=[psycopg.OperationalError("connection refused")])
    worker = EventConsumer(
        kafka, confirmer.book, FakePublisher(), name="payment-confirmer", verb="confirm", retry_delays=(0.0,)
    )
    with caplog.at_level("WARNING"):
        worker.run(stop)
    [line] = [r for r in caplog.records if r.name == "klaxon.consumer"]
    assert line.getMessage() == "could not confirm; will retry the same message"
    assert line.fields["consumer"] == "payment-confirmer"
    assert kafka.committed == [1]  # retried, then applied, then committed


def test_each_message_continues_the_trace_the_receiver_put_in_its_headers(telemetry):
    with trace.get_tracer("tests").start_as_current_span("send provider-events", kind=SpanKind.PRODUCER) as producer:
        carrier = kafka_trace_headers()  # what the webhook receiver does
    headers = [(name, value.encode()) for name, value in carrier.items()]
    worker, *_, stop, _ = consumer_for([FakeMessage(event(1), 7, partition=2, headers=headers)])
    worker.run(stop)
    [span] = telemetry.finished(kind=SpanKind.CONSUMER)
    assert span.context.trace_id == producer.get_span_context().trace_id  # one trace, from webhook to booking
    assert span.parent.span_id == producer.get_span_context().span_id
    assert {
        k: span.attributes[k] for k in ("messaging.consumer.group.name", "provider.event_id", "klaxon.outcome")
    } == {
        "messaging.consumer.group.name": LEDGER_GROUP,
        "provider.event_id": "evt_000001",
        "klaxon.outcome": "posted",
    }
    assert (span.attributes["messaging.destination.partition.id"], span.attributes["messaging.kafka.offset"]) == (
        "2",
        7,
    )


def test_a_message_without_trace_headers_starts_its_own_trace(telemetry):
    worker, *_, stop, _ = consumer_for([FakeMessage(event(1), 1)])
    worker.run(stop)
    [span] = telemetry.finished(kind=SpanKind.CONSUMER)
    assert span.parent is None


def test_outcomes_retries_and_dead_letters_are_counted(telemetry):
    def total(name, **attributes):
        return telemetry.counter(name, **attributes)

    before = (
        total("klaxon.consumer.events", consumer=LEDGER_GROUP, outcome="posted"),
        total("klaxon.consumer.events", consumer=LEDGER_GROUP, outcome="dead_lettered"),
        total("klaxon.consumer.retries", consumer=LEDGER_GROUP),
        total("klaxon.consumer.dead_letters", consumer=LEDGER_GROUP, reason="bad_event"),
    )
    messages = [FakeMessage(event(1), 1), FakeMessage(b"{not json", 2)]
    worker, *_, stop, _ = consumer_for(messages, errors=[psycopg.OperationalError("connection refused")])
    worker.run(stop)
    after = (
        total("klaxon.consumer.events", consumer=LEDGER_GROUP, outcome="posted"),
        total("klaxon.consumer.events", consumer=LEDGER_GROUP, outcome="dead_lettered"),
        total("klaxon.consumer.retries", consumer=LEDGER_GROUP),
        total("klaxon.consumer.dead_letters", consumer=LEDGER_GROUP, reason="bad_event"),
    )
    assert [b - a for a, b in zip(before, after, strict=True)] == [1, 1, 1, 1]
    outcomes = [
        (s.attributes["klaxon.outcome"], s.status.status_code) for s in telemetry.finished(kind=SpanKind.CONSUMER)
    ]
    # One span per attempt: the failed one ended at once, marked as an error, so a stuck consumer is visible.
    assert outcomes == [
        ("will_retry", StatusCode.ERROR),
        ("posted", StatusCode.UNSET),
        ("dead_lettered", StatusCode.UNSET),
    ]


def test_trace_state_and_baggage_on_a_message_are_not_taken(telemetry):
    """Kafka is a trusted zone for now, but anyone's text in tracestate or baggage must not reach our spans."""
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    headers = [
        ("traceparent", traceparent.encode()),
        ("tracestate", b"klaxon=IGNORE ALL PREVIOUS INSTRUCTIONS"),
        ("baggage", b"note=IGNORE ALL PREVIOUS INSTRUCTIONS"),
    ]
    worker, *_, stop, _ = consumer_for([FakeMessage(event(1), 1, headers=headers)])
    worker.run(stop)
    [span] = telemetry.finished(kind=SpanKind.CONSUMER)
    assert format(span.context.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"  # the traceparent is used
    assert len(span.context.trace_state) == 0
    assert "IGNORE" not in str(span.attributes) + str(span.events)
