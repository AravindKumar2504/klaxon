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
from collections.abc import Iterable, Mapping

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
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

logger = logging.getLogger("klaxon.kafka")

EVENTS_TOPIC = "provider-events"
DLQ_TOPIC = "provider-events-dlq"
LEDGER_GROUP = "ledger"
CONFIRMER_GROUP = "payment-confirmer"
CONSUMER_GROUPS = (LEDGER_GROUP, CONFIRMER_GROUP)  # every group that reads EVENTS_TOPIC


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


def _partitions(admin: AdminClient, topic: str, timeout: float) -> list[int]:
    metadata = admin.list_topics(topic=topic, timeout=timeout)
    if topic not in metadata.topics or metadata.topics[topic].error is not None:
        raise KafkaException(f"topic {topic} not found")
    return sorted(metadata.topics[topic].partitions)


def _committed(admin: AdminClient, group_id: str, topic: str, partitions: list[int], timeout: float) -> dict[int, int]:
    request = ConsumerGroupTopicPartitions(group_id, [TopicPartition(topic, p) for p in partitions])
    group = admin.list_consumer_group_offsets([request], request_timeout=timeout)[group_id].result(timeout)
    return {tp.partition: tp.offset for tp in group.topic_partitions}


def _offsets(admin: AdminClient, topic: str, partitions: list[int], spec: OffsetSpec, timeout: float) -> dict[int, int]:
    futures = admin.list_offsets({TopicPartition(topic, p): spec for p in partitions}, request_timeout=timeout)
    return {tp.partition: future.result(timeout).offset for tp, future in futures.items()}


def consumer_lag(
    bootstrap: str, group_id: str, topic: str, timeout: float = 5.0, admin: AdminClient | None = None
) -> dict[int, int]:
    """Per partition: how many messages the group has not committed yet."""
    admin = admin or AdminClient({"bootstrap.servers": bootstrap})
    partitions = _partitions(admin, topic, timeout)
    committed = _committed(admin, group_id, topic, partitions, timeout)
    end = _offsets(admin, topic, partitions, OffsetSpec.latest(), timeout)
    start = _offsets(admin, topic, partitions, OffsetSpec.earliest(), timeout)
    lag = {}
    for p in partitions:
        place = committed.get(p, OFFSET_INVALID)
        lag[p] = end[p] - (place if place >= 0 else start[p])  # nothing committed yet: everything is lag
    return lag


def end_offsets(bootstrap: str, topic: str, timeout: float = 5.0, admin: AdminClient | None = None) -> dict[int, int]:
    """Per partition: the offset the next message will get. Every message written so far is below it."""
    admin = admin or AdminClient({"bootstrap.servers": bootstrap})
    return _offsets(admin, topic, _partitions(admin, topic, timeout), OffsetSpec.latest(), timeout)


def not_yet_handled(
    bootstrap: str,
    group_id: str,
    topic: str,
    below: Mapping[int, int],
    timeout: float = 5.0,
    admin: AdminClient | None = None,
) -> dict[int, int]:
    """Per partition: how many of the messages below these offsets the group has not committed yet.

    Lag counts up to the end of the log, and the end keeps moving while traffic flows. This counts up to a
    fixed point instead, so a group that keeps up with the traffic does reach it.
    """
    admin = admin or AdminClient({"bootstrap.servers": bootstrap})
    partitions = sorted(below)
    committed = _committed(admin, group_id, topic, partitions, timeout)
    start = _offsets(admin, topic, partitions, OffsetSpec.earliest(), timeout)
    left = {}
    for p in partitions:
        place = committed.get(p, OFFSET_INVALID)
        left[p] = max(0, below[p] - (place if place >= 0 else start[p]))  # nothing committed yet: from the start
    return left


LAG_TIMEOUT_SECONDS = 2.0  # measured on the metrics thread: with Kafka down, give up fast


def lag_observations(
    bootstrap: str, groups: Iterable[str], topic: str, admin: AdminClient | None, lag=consumer_lag
) -> list[Observation]:
    """Each group's lag per partition, as metric values.

    It stops at the first group it cannot measure: that means Kafka itself is unreachable, and waiting for
    every other group would hold up the service's other metrics during exactly that outage.
    """
    out = []
    for group in groups:
        try:
            by_partition = lag(bootstrap, group, topic, timeout=LAG_TIMEOUT_SECONDS, admin=admin)
        except Exception as exc:  # no value this time, rather than a wrong one
            logger.warning("could not measure consumer lag", extra={"fields": {"group": group, "error": str(exc)}})
            return out
        out += [Observation(behind, {"consumer": group, "partition": str(p)}) for p, behind in by_partition.items()]
    return out


def observe_consumer_lag(bootstrap: str, groups: Iterable[str] = CONSUMER_GROUPS, topic: str = EVENTS_TOPIC) -> None:
    """Report every group's lag as a metric, measured from outside the consumers: from the committed offsets.

    A consumer stuck retrying one message still shows a growing lag, and a stopped one too, because the
    measurement does not depend on the consumer itself. It runs each time the metrics are exported.
    """
    admin = AdminClient({"bootstrap.servers": bootstrap})
    groups = tuple(groups)

    def measure(options: CallbackOptions) -> list[Observation]:
        return lag_observations(bootstrap, groups, topic, admin)

    metrics.get_meter("klaxon.kafka").create_observable_gauge(
        "klaxon.kafka.consumer.lag",
        callbacks=[measure],
        unit="{message}",
        description="Messages in provider-events that a consumer group has not committed yet, per partition.",
    )
