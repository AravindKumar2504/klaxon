"""The Kafka helpers against the stack's real broker: acknowledged writes, keys, and lag."""

import uuid

import pytest
from confluent_kafka import Consumer
from confluent_kafka.admin import AdminClient, NewTopic

from klaxon.patient.services.kafka_io import KafkaPublisher, consumer_lag, end_offsets, make_consumer, not_yet_handled
from tests.integration.conftest import KAFKA

pytestmark = pytest.mark.integration


@pytest.fixture
def topic():
    admin = AdminClient({"bootstrap.servers": KAFKA})
    name = f"klaxon-test-{uuid.uuid4().hex[:8]}"
    admin.create_topics([NewTopic(name, num_partitions=3, replication_factor=1)])[name].result(15)
    yield name
    admin.delete_topics([name])[name].result(15)


def drain(consumer: Consumer, n: int) -> list:
    messages = []
    while len(messages) < n:
        msg = consumer.poll(10.0)
        assert msg is not None, f"only {len(messages)} of {n} messages arrived"
        assert msg.error() is None, msg.error()
        messages.append(msg)
    return messages


def test_an_acknowledged_write_is_readable_with_its_key_and_headers(topic):
    publisher = KafkaPublisher(KAFKA, client_id="test-publisher")
    try:
        partition, offset = publisher.publish(topic, "pay_000001", b'{"id":"evt_1"}', {"received-at": "now"})
    finally:
        publisher.close()
    consumer = make_consumer(KAFKA, f"test-{uuid.uuid4().hex[:8]}", "test-consumer")
    consumer.subscribe([topic])
    try:
        [msg] = drain(consumer, 1)
    finally:
        consumer.close()
    assert (msg.partition(), msg.offset(), msg.key(), msg.value()) == (
        partition,
        offset,
        b"pay_000001",
        b'{"id":"evt_1"}',
    )
    assert dict(msg.headers()) == {"received-at": b"now"}


def test_the_same_key_always_lands_on_the_same_partition(topic):
    publisher = KafkaPublisher(KAFKA, client_id="test-publisher")
    try:
        partitions = {publisher.publish(topic, "pay_000042", f"{n}".encode())[0] for n in range(5)}
    finally:
        publisher.close()
    assert len(partitions) == 1


def test_lag_counts_what_the_group_has_not_committed(topic):
    publisher = KafkaPublisher(KAFKA, client_id="test-publisher")
    try:
        for n in range(6):
            publisher.publish(topic, f"pay_{n}", b"x")
    finally:
        publisher.close()
    group = f"test-{uuid.uuid4().hex[:8]}"
    assert sum(consumer_lag(KAFKA, group, topic).values()) == 6  # a new group has committed nothing

    consumer = make_consumer(KAFKA, group, "test-consumer")
    consumer.subscribe([topic])
    try:
        messages = drain(consumer, 6)
        for msg in messages[:4]:
            consumer.commit(message=msg, asynchronous=False)
        assert sum(consumer_lag(KAFKA, group, topic).values()) == 2
        for msg in messages[4:]:
            consumer.commit(message=msg, asynchronous=False)
        assert sum(consumer_lag(KAFKA, group, topic).values()) == 0
    finally:
        consumer.close()


def test_handled_up_to_a_point_is_counted_up_to_that_point_however_much_arrives_after(topic):
    """What the money check waits for under traffic: the messages before a fixed point, not the end of the log."""
    publisher = KafkaPublisher(KAFKA, client_id="test-publisher")
    try:
        for n in range(6):
            publisher.publish(topic, f"pay_{n}", b"before")
        point = end_offsets(KAFKA, topic)
        for n in range(4):
            publisher.publish(topic, f"pay_{n}", b"after")  # new traffic after the point
    finally:
        publisher.close()
    assert sum(point.values()) == 6
    group = f"test-{uuid.uuid4().hex[:8]}"
    assert sum(not_yet_handled(KAFKA, group, topic, point).values()) == 6  # a new group: everything is left

    consumer = make_consumer(KAFKA, group, "test-consumer")
    consumer.subscribe([topic])
    try:
        for msg in drain(consumer, 10):
            if msg.offset() < point[msg.partition()]:
                consumer.commit(message=msg, asynchronous=False)
        assert not_yet_handled(KAFKA, group, topic, point) == {p: 0 for p in point}
        assert sum(consumer_lag(KAFKA, group, topic).values()) == 4  # the newer messages still count as lag
    finally:
        consumer.close()


def test_publishing_to_a_topic_that_does_not_exist_fails_instead_of_creating_it():
    publisher = KafkaPublisher(KAFKA, client_id="test-publisher", ack_timeout_seconds=6.0)
    try:
        from klaxon.patient.services.kafka_io import PublishError

        with pytest.raises(PublishError):
            publisher.publish(f"no-such-topic-{uuid.uuid4().hex[:6]}", "k", b"x")
    finally:
        publisher.close()
