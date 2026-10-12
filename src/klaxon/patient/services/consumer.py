"""The Kafka consumer loop: read an event, apply it in PostgreSQL, then commit the offset.

The ledger consumer runs it (ledger_consumer), where "apply" means "book the
money". The loop lives here, apart from the ledger, so a second consumer can
follow the same rules.

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
from psycopg_pool import ConnectionPool

from klaxon.patient.services.common import Heartbeat, env_str, log
from klaxon.patient.services.events import BadEvent, ProviderEvent, parse_event
from klaxon.patient.services.kafka_io import DLQ_TOPIC, EVENTS_TOPIC, KafkaPublisher, make_consumer

logger = logging.getLogger("klaxon.consumer")

RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)
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
    ) -> None:
        self.consumer = consumer
        self.apply = apply
        self.dead_letters = dead_letters
        self.name = name
        self.verb = verb
        self.dlq_topic = dlq_topic
        self.heartbeat = heartbeat or Heartbeat(None)
        self.retry_delays = retry_delays

    def run(self, stop: threading.Event) -> None:
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
        source = f"{msg.topic()}/{msg.partition()}@{msg.offset()}"
        try:
            event = parse_event(msg.value())
        except BadEvent as exc:
            return self._dead_letter(msg, source, f"cannot {self.verb} this event: {exc}", stop)

        attempt = 0
        while True:
            try:
                outcome = self.apply(event, source)
            except REFUSED_VALUES as exc:
                reason = f"database refused the values: {type(exc).__name__}: {exc}"
                return self._dead_letter(msg, source, reason, stop)
            except Exception as exc:  # down, permissions, schema, a bug: ours to fix, so wait and retry
                delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                attempt += 1
                log(
                    logger,
                    logging.ERROR if attempt >= 3 else logging.WARNING,
                    f"could not {self.verb}; will retry the same message",
                    consumer=self.name,
                    event_id=event.id,
                    source=source,
                    attempt=attempt,
                    retry_in_seconds=delay,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if stop.wait(delay):
                    return False  # shutting down: leave it uncommitted, it will be read again
                continue
            log(
                logger,
                logging.INFO,
                outcome.replace("_", " "),
                consumer=self.name,
                event_id=event.id,
                event_type=event.type,
                payment_id=event.payment_id,
                amount_cents=event.amount_cents,
                source=source,
            )
            return True

    def _dead_letter(self, msg: Any, source: str, reason: str, stop: threading.Event) -> bool:
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
                    return False
                continue
            log(logger, logging.ERROR, "dead-lettered", consumer=self.name, source=source, reason=reason)
            return True

    def _commit(self, msg: Any) -> None:
        try:
            self.consumer.commit(message=msg, asynchronous=False)
        except KafkaException as exc:
            # Safe to carry on: the message will be read again and applied as a duplicate, which is a no-op.
            log(logger, logging.WARNING, "offset commit failed", consumer=self.name, error=str(exc))


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
        ).run(stop)
    finally:
        consumer.close()  # leaves the group cleanly, so its partitions move to another consumer at once
        dead_letters.close()
