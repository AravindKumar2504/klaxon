"""Kafka helpers: a publisher that waits for the broker's acknowledgement, a consumer, and lag.

Topics (created by the kafka-init container in compose.yaml, never on the fly):
- provider-events: every webhook the receiver accepted, keyed by payment id,
  3 partitions. One payment's events share a partition, so they stay in order
  relative to each other; different payments are spread across partitions.
- provider-events-dlq: events a consumer could not use, with the reason and
  the consumer's name in headers, kept for a human to look at.

Consumer groups: Kafka splits the topic's partitions among a group's running
consumers and remembers, per partition, the offset the group has committed
(its place in the log). Lag is how far that place is behind the end. Two
groups read provider-events, each with its own place: "ledger" books the
money, "payment-confirmer" marks our payments succeeded. One can fall behind
or stop while the other keeps going.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping

from confluent_kafka import (
    OFFSET_INVALID,
    Consumer,
    ConsumerGroupTopicPartitions,
    KafkaError,
    KafkaException,
    Producer,
    TopicPartition,
)
from confluent_kafka.admin import AdminClient, OffsetSpec

logger = logging.getLogger("klaxon.kafka")

EVENTS_TOPIC = "provider-events"
DLQ_TOPIC = "provider-events-dlq"
LEDGER_GROUP = "ledger"
CONFIRMER_GROUP = "payment-confirmer"


class PublishError(Exception):
    """Kafka did not confirm the write. The caller must not acknowledge the event."""


class KafkaPublisher:
    def __init__(self, bootstrap: str, client_id: str, ack_timeout_seconds: float = 4.0) -> None:
        self.ack_timeout_seconds = ack_timeout_seconds
        self._config = {
            "bootstrap.servers": bootstrap,
            "client.id": client_id,
            # The broker drops duplicates caused by the producer's own internal
            # retries. (Duplicates from the provider retrying are a different
            # thing; the ledger consumer handles those.)
            "enable.idempotence": True,
            "acks": "all",
            "linger.ms": 0,
            "request.timeout.ms": 2000,
            # Give up within 3 s, so the receiver can answer 503 before the
            # provider's own 5 s timeout, and the provider retries.
            "delivery.timeout.ms": 3000,
            "error_cb": self._on_error,
        }
        self._fatal: str | None = None  # set when librdkafka says this producer can never send again
        self._swap = threading.Lock()
        self._producer = Producer(self._config)
        self._stop = threading.Event()
        # Delivery reports arrive through poll(); one thread keeps polling.
        self._poller = threading.Thread(target=self._poll, name="kafka-producer-poll", daemon=True)
        self._poller.start()

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._producer.poll(0.1)

    def _on_error(self, err: KafkaError) -> None:
        if err.fatal():
            # For example an idempotent-producer sequence error. librdkafka's advice: make a new producer.
            self._fatal = str(err)
            logger.error("kafka producer hit a fatal error; it will be replaced", extra={"fields": {"error": str(err)}})
        else:
            logger.warning("kafka client error", extra={"fields": {"error": str(err)}})

    def _replace_if_fatal(self) -> None:
        with self._swap:
            if self._fatal is None:
                return
            self._producer = Producer(self._config)  # the poll thread picks up the new one
            self._fatal = None

    def publish(self, topic: str, key: str, value: bytes, headers: Mapping[str, str] | None = None) -> tuple[int, int]:
        """Write one message and wait for the broker's acknowledgement. Returns (partition, offset)."""
        self._replace_if_fatal()
        done = threading.Event()
        report: dict = {}

        def on_delivery(err: object, msg: object) -> None:
            report["err"], report["msg"] = err, msg
            done.set()

        try:
            self._producer.produce(
                topic,
                key=key.encode(),
                value=value,
                headers=[(name, text.encode()) for name, text in (headers or {}).items()],
                on_delivery=on_delivery,
            )
        except (BufferError, KafkaException) as exc:
            raise PublishError(f"could not queue the message: {exc}") from exc
        if not done.wait(self.ack_timeout_seconds):
            raise PublishError("no acknowledgement from Kafka in time")
        if report["err"] is not None:
            raise PublishError(str(report["err"]))
        return report["msg"].partition(), report["msg"].offset()

    def healthy(self, timeout: float = 2.0) -> bool:
        if self._fatal is not None:
            return False
        try:
            self._producer.list_topics(timeout=timeout)
        except KafkaException:
            return False
        return True

    def close(self) -> None:
        self._stop.set()
        self._poller.join(2.0)
        self._producer.flush(5.0)


def make_consumer(bootstrap: str, group_id: str, client_id: str) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group_id,
            "client.id": client_id,
            # We commit after the database commit, never before (see consumer).
            "enable.auto.commit": False,
            # A group with no saved place starts at the beginning, so nothing is skipped.
            "auto.offset.reset": "earliest",
            "session.timeout.ms": 10_000,
        }
    )


def consumer_lag(bootstrap: str, group_id: str, topic: str, timeout: float = 5.0) -> dict[int, int]:
    """Per partition: how many messages the group has not committed yet."""
    admin = AdminClient({"bootstrap.servers": bootstrap})
    metadata = admin.list_topics(topic=topic, timeout=timeout)
    if topic not in metadata.topics or metadata.topics[topic].error is not None:
        raise KafkaException(f"topic {topic} not found")
    partitions = sorted(metadata.topics[topic].partitions)
    request = ConsumerGroupTopicPartitions(group_id, [TopicPartition(topic, p) for p in partitions])
    group = admin.list_consumer_group_offsets([request], request_timeout=timeout)[group_id].result(timeout)
    committed = {tp.partition: tp.offset for tp in group.topic_partitions}

    def offsets(spec: OffsetSpec) -> dict[int, int]:
        futures = admin.list_offsets({TopicPartition(topic, p): spec for p in partitions}, request_timeout=timeout)
        return {tp.partition: future.result(timeout).offset for tp, future in futures.items()}

    end, start = offsets(OffsetSpec.latest()), offsets(OffsetSpec.earliest())
    lag = {}
    for p in partitions:
        place = committed.get(p, OFFSET_INVALID)
        lag[p] = end[p] - (place if place >= 0 else start[p])  # nothing committed yet: everything is lag
    return lag
