"""The loop both Kafka consumers share: read an event, apply it in PostgreSQL, then commit the offset.

Two consumer groups read the provider-events topic, each with its own place in
the log: "ledger" books the money (ledger_consumer), "payment-confirmer" marks
our payments succeeded (payment_confirmer). Each plugs in what "apply" means;
the rules below are the same for both.

Kafka delivers at least once too. If a consumer applies an event and crashes
before it saves its place (commits its offset), it reads that event again
after a restart. So the order is always: apply in PostgreSQL (one
transaction), then commit the offset. Read twice + idempotent apply = applied
once.

What happens to each message:
- applied, or already applied (a duplicate): commit the offset, move on.
- not usable as it is (no body, bad JSON, missing or malformed fields, wrong
  currency, an absurd amount), or values the database refuses (DataError,
  IntegrityError, ProgramLimitExceeded, a string it cannot encode): send it to
  the dead-letter topic with the reason and the consumer's name, commit, move
  on. Never drop it silently, and never let one bad message block the
  partition behind it.
- any other problem (the database is down, a permission or schema problem, a
  bug in our rules): retry the same message with backoff, without committing,
  until it works. A money event is never skipped because something on our
  side is broken; the growing lag is what an alert watches.

A known-bad baseline for the poison-message fault (week 2): BAD_EVENTS=retry
makes a consumer retry a message it can never use, forever, instead of
dead-lettering it. One consumer reads all its partitions in one loop, so it
then books nothing at all (head-of-line blocking). The default is
BAD_EVENTS=dead_letter.

Telemetry (week 2): each attempt at a message gets its own span, which
continues the trace the webhook receiver put in the message's headers, so the
work shows up under the webhook that brought it. A failed attempt's span ends
at once, marked as an error, so a consumer retrying for an hour shows every
failure as it happens. Counters: klaxon.consumer.events (by outcome, dead
letters included), klaxon.consumer.retries and klaxon.consumer.dead_letters.
The last two start at 0 for each consumer, so a dashboard shows a real 0
rather than "No data", and goes blank only if the metrics themselves stop.
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import psycopg
from confluent_kafka import KafkaException
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, StatusCode
from psycopg_pool import ConnectionPool

from klaxon.patient.services.common import Heartbeat, capped, env_str, log
from klaxon.patient.services.events import BadEvent, ProviderEvent, parse_event
from klaxon.patient.services.kafka_io import DLQ_TOPIC, EVENTS_TOPIC, KafkaPublisher, make_consumer
from klaxon.patient.services.telemetry import trace_from_kafka

logger = logging.getLogger("klaxon.consumer")
tracer = trace.get_tracer("klaxon.consumer")
meter = metrics.get_meter("klaxon.consumer")
EVENTS = meter.create_counter(
    "klaxon.consumer.events", unit="{message}", description="Messages a consumer finished, by outcome."
)
RETRIES = meter.create_counter(
    "klaxon.consumer.retries",
    unit="{attempt}",
    description="Failed attempts to apply an event; the same message is tried again. A stuck consumer climbs.",
)
DEAD_LETTERS = meter.create_counter(
    "klaxon.consumer.dead_letters", unit="{message}", description="Messages sent to the dead-letter topic, by why."
)

RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)
# What to do with a message that can never be applied: dead-letter it (the default), or the known-bad
# baseline, retry it forever.
BAD_EVENT_POLICIES = ("dead_letter", "retry")
DEAD_LETTER_REASONS = ("bad_event", "refused_values")
# The database refuses these values; retrying the same message cannot change that.
REFUSED_VALUES = (psycopg.DataError, psycopg.IntegrityError, psycopg.errors.ProgramLimitExceeded, UnicodeError)

# Apply one event; returns what happened ("posted", "confirmed", ...), which is logged.
Apply = Callable[[ProviderEvent, str], str]


class DeadLetters(Protocol):
    def publish(
        self, topic: str, key: str, value: bytes, headers: Mapping[str, str] | None = None
    ) -> tuple[int, int]: ...


class EventConsumer:
    def __init__(
        self,
        consumer: Any,  # a confluent_kafka.Consumer, already subscribed
        apply: Apply,
        dead_letters: DeadLetters,
        *,
        name: str,  # the consumer group, written on its dead letters
        verb: str,  # what apply does, for the logs: "book", "confirm"
        dlq_topic: str = DLQ_TOPIC,
        heartbeat: Heartbeat | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
        bad_events: str = "dead_letter",
    ) -> None:
        if bad_events not in BAD_EVENT_POLICIES:
            raise ValueError(f"unknown bad-event policy {bad_events!r}; expected one of {BAD_EVENT_POLICIES}")
        self.bad_events = bad_events
        self.consumer = consumer
        self.apply = apply
        self.dead_letters = dead_letters
        self.name = name
        self.verb = verb
        self.dlq_topic = dlq_topic
        self.heartbeat = heartbeat or Heartbeat(None)
        self.retry_delays = retry_delays

    def run(self, stop: threading.Event) -> None:
        RETRIES.add(0, {"consumer": self.name})  # every series starts at 0: "no retries", not "no data"
        for reason in DEAD_LETTER_REASONS:
            DEAD_LETTERS.add(0, {"consumer": self.name, "reason": reason})
        while not stop.is_set():
            self.heartbeat.beat()
            msg = self.consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error() is not None:
                log(logger, logging.WARNING, "kafka error", consumer=self.name, error=str(msg.error()))
                continue
            if self.handle(msg, stop):
                self._commit(msg)

    def handle(self, msg: Any, stop: threading.Event) -> bool:
        """Process one message. True when it is done and its offset may be committed."""
        outcome = self._process(msg, stop)
        if outcome is None:
            return False  # shutting down: leave it uncommitted, it will be read again
        EVENTS.add(1, {"consumer": self.name, "outcome": outcome})
        return True

    def _attempt(self, msg: Any, parent: Context, event: ProviderEvent | None = None):
        """A span for one try at the message, under the webhook's trace."""
        attributes = {
            "messaging.system": "kafka",
            "messaging.operation.type": "process",
            "messaging.destination.name": msg.topic(),
            "messaging.destination.partition.id": str(msg.partition()),
            "messaging.kafka.offset": msg.offset(),
            "messaging.consumer.group.name": self.name,
        }
        if event is not None:
            attributes |= {"provider.event_id": event.id, "provider.payment_id": event.payment_id}
            if event.klaxon_payment_id is not None:
                attributes["klaxon.payment_id"] = event.klaxon_payment_id
        return tracer.start_as_current_span(
            f"process {msg.topic()}", context=parent, kind=SpanKind.CONSUMER, attributes=attributes
        )

    def _process(self, msg: Any, stop: threading.Event) -> str | None:
        """What happened to the message ("posted", "dead_lettered", ...), or None when stopped before it was done."""
        source = f"{msg.topic()}/{msg.partition()}@{msg.offset()}"
        parent = trace_from_kafka(msg.headers())  # the webhook's trace
        try:
            event = parse_event(msg.value())
        except BadEvent as exc:
            reason = f"cannot {self.verb} this event: {exc}"
            if self.bad_events == "retry":
                return self._retry_forever(msg, parent, source, reason, stop)
            with self._attempt(msg, parent) as span:
                return _finish(span, self._dead_letter(msg, source, reason, "bad_event", stop))

        attempt = 0
        while True:
            with self._attempt(msg, parent, event) as span:
                try:
                    outcome = self.apply(event, source)
                except REFUSED_VALUES as exc:
                    reason = f"database refused the values: {type(exc).__name__}: {exc}"
                    if self.bad_events == "dead_letter":
                        return _finish(span, self._dead_letter(msg, source, reason, "refused_values", stop))
                    error = reason  # the known-bad baseline: retried like an outage, forever
                    span.set_status(StatusCode.ERROR, capped(error))
                    span.set_attribute("klaxon.outcome", "will_retry")
                except Exception as exc:  # down, permissions, schema, a bug: ours to fix, so wait and retry
                    error = f"{type(exc).__name__}: {exc}"
                    span.set_status(StatusCode.ERROR, capped(error))
                    span.set_attribute("klaxon.outcome", "will_retry")
                else:
                    log(
                        logger,
                        logging.INFO,
                        outcome.replace("_", " "),
                        consumer=self.name,
                        event_id=event.id,
                        event_type=event.type,
                        payment_id=event.klaxon_payment_id,  # ours, as in the payments API's lines
                        provider_payment_id=event.payment_id,
                        amount_cents=event.amount_cents,
                        source=source,
                    )
                    return _finish(span, outcome)
            delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
            attempt += 1
            RETRIES.add(1, {"consumer": self.name})
            log(
                logger,
                logging.ERROR if attempt >= 3 else logging.WARNING,
                f"could not {self.verb}; will retry the same message",
                consumer=self.name,
                event_id=event.id,
                source=source,
                attempt=attempt,
                retry_in_seconds=delay,
                error=error,
            )
            if stop.wait(delay):
                return None  # shutting down: leave it uncommitted, it will be read again

    def _retry_forever(self, msg: Any, parent: Context, source: str, reason: str, stop: threading.Event) -> None:
        """The known-bad baseline for a message no retry can fix: try it again, forever. Nothing behind it moves."""
        attempt = 0
        while True:
            with self._attempt(msg, parent) as span:
                span.set_status(StatusCode.ERROR, capped(reason))
                span.set_attribute("klaxon.outcome", "will_retry")
            delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
            attempt += 1
            RETRIES.add(1, {"consumer": self.name})
            log(
                logger,
                logging.ERROR if attempt >= 3 else logging.WARNING,
                f"could not {self.verb}; will retry the same message",
                consumer=self.name,
                source=source,
                attempt=attempt,
                retry_in_seconds=delay,
                error=reason,
            )
            if stop.wait(delay):
                return None  # shutting down: leave it uncommitted, it will be read again

    def _dead_letter(self, msg: Any, source: str, reason: str, kind: str, stop: threading.Event) -> str | None:
        key = msg.key().decode(errors="replace") if msg.key() else ""
        headers = {"reason": reason[:500], "source": source, "consumer": self.name}
        attempt = 0
        while True:
            try:
                self.dead_letters.publish(self.dlq_topic, key, msg.value() or b"", headers)
            except Exception as exc:
                delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                attempt += 1
                log(
                    logger,
                    logging.ERROR,
                    "could not dead-letter; will retry",
                    consumer=self.name,
                    source=source,
                    error=str(exc),
                )
                if stop.wait(delay):
                    return None
                continue
            DEAD_LETTERS.add(1, {"consumer": self.name, "reason": kind})
            log(logger, logging.ERROR, "dead-lettered", consumer=self.name, source=source, reason=reason)
            return "dead_lettered"

    def _commit(self, msg: Any) -> None:
        try:
            self.consumer.commit(message=msg, asynchronous=False)
        except KafkaException as exc:
            # Safe to carry on: the message will be read again and applied as a duplicate, which is a no-op.
            log(logger, logging.WARNING, "offset commit failed", consumer=self.name, error=str(exc))


def _finish(span: Span, outcome: str | None) -> str | None:
    span.set_attribute("klaxon.outcome", outcome or "interrupted")
    return outcome


def open_pool(name: str) -> ConnectionPool:
    """A small pool to DATABASE_URL, the way both consumers use the database."""
    return ConnectionPool(
        env_str("DATABASE_URL"),
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True},  # transaction() blocks make the transactions
        name=name,
        timeout=5.0,  # with the database down, fail within 5 s and go through the logged retry loop
        check=ConnectionPool.check_connection,  # after a PostgreSQL restart, drop dead connections first
        open=True,
    )


def serve(apply: Apply, *, group: str, verb: str, heartbeat_path: str) -> None:
    """Run one consumer until SIGTERM: Kafka from KAFKA_BOOTSTRAP, the group's place in the log, dead letters."""
    bootstrap = env_str("KAFKA_BOOTSTRAP", "kafka:9092")
    topic = env_str("KAFKA_TOPIC", EVENTS_TOPIC)
    group = env_str("KAFKA_GROUP", group)
    bad_events = env_str("BAD_EVENTS", "dead_letter")
    log(logger, logging.INFO, "consumer settings", consumer=group, bad_events=bad_events)
    consumer = make_consumer(bootstrap, group, client_id=f"{group}-{socket.gethostname()}")

    def assignment(kind: str) -> Callable[[Any, list], None]:
        def callback(consumer: Any, partitions: list) -> None:
            log(
                logger,
                logging.INFO,
                f"partitions {kind}",
                consumer=group,
                partitions=sorted(p.partition for p in partitions),
            )

        return callback

    consumer.subscribe([topic], on_assign=assignment("assigned"), on_revoke=assignment("revoked"))
    dead_letters = KafkaPublisher(bootstrap, client_id=f"{group}-dead-letters")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        EventConsumer(
            consumer,
            apply,
            dead_letters,
            name=group,
            verb=verb,
            dlq_topic=env_str("KAFKA_DLQ_TOPIC", DLQ_TOPIC),
            heartbeat=Heartbeat(env_str("HEARTBEAT_PATH", heartbeat_path)),
            bad_events=bad_events,
        ).run(stop)
    finally:
        consumer.close()  # leaves the group cleanly, so its partitions move to another consumer at once
        dead_letters.close()
