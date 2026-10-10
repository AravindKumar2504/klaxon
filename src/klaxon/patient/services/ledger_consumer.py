"""The ledger consumer: reads provider events from Kafka and books them in PostgreSQL.

Kafka delivers at least once too. If the consumer books an event and crashes
before it saves its place (commits its offset), it reads that event again
after a restart. So the order is always: book in PostgreSQL (one transaction),
then commit the offset. Read twice + idempotent booking = booked once.

What happens to each message:
- booked, or already booked (a duplicate): commit the offset, move on.
- not bookable as it is (bad JSON, missing fields, wrong currency, a value the
  database refuses): send it to the dead-letter topic with the reason, commit,
  move on. Never drop it silently.
- any other database problem (down, permissions, a broken schema): retry the
  same message with backoff, without committing, until it works. A money event
  is never skipped because something on our side is broken; the growing lag is
  what an alert watches (week 2).

Run: python -m klaxon.patient.services.ledger_consumer
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
from collections.abc import Mapping
from typing import Any, Protocol

import psycopg
from confluent_kafka import KafkaException
from psycopg_pool import ConnectionPool

from klaxon.patient.services.common import Heartbeat, configure_logging, env_str, log
from klaxon.patient.services.events import BadEvent, ProviderEvent, parse_event
from klaxon.patient.services.kafka_io import DLQ_TOPIC, EVENTS_TOPIC, LEDGER_GROUP, KafkaPublisher, make_consumer
from klaxon.patient.services.pg_ledger import PgLedger

logger = logging.getLogger("klaxon.ledger_consumer")

RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)


class Booker(Protocol):
    def book(self, event: ProviderEvent, source: str) -> str: ...


class DeadLetters(Protocol):
    def publish(
        self, topic: str, key: str, value: bytes, headers: Mapping[str, str] | None = None
    ) -> tuple[int, int]: ...


class LedgerConsumer:
    def __init__(
        self,
        consumer: Any,  # a confluent_kafka.Consumer, already subscribed
        booker: Booker,
        dead_letters: DeadLetters,
        dlq_topic: str = DLQ_TOPIC,
        heartbeat: Heartbeat | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
    ) -> None:
        self.consumer = consumer
        self.booker = booker
        self.dead_letters = dead_letters
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
                log(logger, logging.WARNING, "kafka error", error=str(msg.error()))
                continue
            if self.handle(msg, stop):
                self._commit(msg)

    def handle(self, msg: Any, stop: threading.Event) -> bool:
        """Process one message. True when it is done and its offset may be committed."""
        source = f"{msg.topic()}/{msg.partition()}@{msg.offset()}"
        try:
            event = parse_event(msg.value())
        except BadEvent as exc:
            return self._dead_letter(msg, source, f"not bookable: {exc}", stop)

        attempt = 0
        while True:
            try:
                outcome = self.booker.book(event, source)
            except psycopg.DataError as exc:  # this value cannot be stored; retrying will not change that
                return self._dead_letter(msg, source, f"database refused the values: {exc}", stop)
            except Exception as exc:  # down, permissions, schema: ours to fix, so wait and retry
                delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                attempt += 1
                log(
                    logger,
                    logging.ERROR if attempt >= 3 else logging.WARNING,
                    "could not book; will retry the same message",
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
                event_id=event.id,
                event_type=event.type,
                payment_id=event.payment_id,
                amount_cents=event.amount_cents,
                source=source,
            )
            return True

    def _dead_letter(self, msg: Any, source: str, reason: str, stop: threading.Event) -> bool:
        key = msg.key().decode(errors="replace") if msg.key() else ""
        headers = {"reason": reason[:500], "source": source}
        attempt = 0
        while True:
            try:
                self.dead_letters.publish(self.dlq_topic, key, msg.value() or b"", headers)
            except Exception as exc:
                delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                attempt += 1
                log(logger, logging.ERROR, "could not dead-letter; will retry", source=source, error=str(exc))
                if stop.wait(delay):
                    return False
                continue
            log(logger, logging.ERROR, "dead-lettered", source=source, reason=reason)
            return True

    def _commit(self, msg: Any) -> None:
        try:
            self.consumer.commit(message=msg, asynchronous=False)
        except KafkaException as exc:
            # Safe to carry on: the message will be read again and booked as a duplicate, which is a no-op.
            log(logger, logging.WARNING, "offset commit failed", error=str(exc))


def _log_assignment(kind: str) -> Any:
    def callback(consumer: Any, partitions: list) -> None:
        log(logger, logging.INFO, f"partitions {kind}", partitions=sorted(p.partition for p in partitions))

    return callback


def main() -> None:
    configure_logging("ledger-consumer")
    mode = env_str("LEDGER_HANDLER", "idempotent")
    bootstrap = env_str("KAFKA_BOOTSTRAP", "kafka:9092")
    topic = env_str("KAFKA_TOPIC", EVENTS_TOPIC)
    pool = ConnectionPool(
        env_str("DATABASE_URL"), min_size=1, max_size=2, kwargs={"autocommit": True}, name="ledger", open=True
    )
    consumer = make_consumer(
        bootstrap, env_str("KAFKA_GROUP", LEDGER_GROUP), client_id=f"ledger-{socket.gethostname()}"
    )
    consumer.subscribe([topic], on_assign=_log_assignment("assigned"), on_revoke=_log_assignment("revoked"))
    dead_letters = KafkaPublisher(bootstrap, client_id="ledger-dead-letters")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log(logger, logging.INFO, "ledger consumer starting", mode=mode, topic=topic)
    try:
        LedgerConsumer(
            consumer,
            PgLedger(pool, mode),
            dead_letters,
            dlq_topic=env_str("KAFKA_DLQ_TOPIC", DLQ_TOPIC),
            heartbeat=Heartbeat(env_str("HEARTBEAT_PATH", "/tmp/ledger-consumer.heartbeat")),
        ).run(stop)
    finally:
        consumer.close()  # leaves the group cleanly, so its partitions move to another consumer at once
        dead_letters.close()
        pool.close()
        log(logger, logging.INFO, "ledger consumer stopped")


if __name__ == "__main__":
    main()
