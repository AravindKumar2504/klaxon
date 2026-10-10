import json
import logging
import threading

from klaxon.patient.services import kafka_io
from klaxon.patient.services.common import MAX_LOGGED_CHARS, JsonFormatter


def test_log_lines_are_json_and_long_strings_are_cut():
    record = logging.LogRecord("klaxon.test", logging.INFO, __file__, 1, "x" * 1000, None, None)
    record.fields = {"reason": "y" * 1000, "attempt": 3}
    line = json.loads(JsonFormatter("test").format(record))
    assert line["service"] == "test" and line["attempt"] == 3
    assert len(line["msg"]) < MAX_LOGGED_CHARS + 50 and line["msg"].endswith("more characters cut]")
    assert len(line["reason"]) < MAX_LOGGED_CHARS + 50


class FakeProducer:
    made: list["FakeProducer"] = []

    def __init__(self, config):
        self.config = config
        self.sent = []
        FakeProducer.made.append(self)

    def produce(self, topic, key, value, headers, on_delivery):
        self.sent.append(value)

        class Msg:
            def partition(self):
                return 0

            def offset(self):
                return len(self_sent) - 1

        self_sent = self.sent
        on_delivery(None, Msg())

    def poll(self, timeout):
        threading.Event().wait(0.01)

    def list_topics(self, timeout):
        return {}

    def flush(self, timeout):
        return 0


class FatalError:
    def fatal(self):
        return True

    def __str__(self):
        return "Fatal error: idempotent producer sequence out of order"


def test_a_producer_with_a_fatal_error_reports_unhealthy_and_is_replaced(monkeypatch):
    monkeypatch.setattr(kafka_io, "Producer", FakeProducer)
    FakeProducer.made.clear()
    publisher = kafka_io.KafkaPublisher("unused:9092", client_id="test")
    try:
        assert publisher.healthy()
        publisher._on_error(FatalError())  # what librdkafka calls when the producer can never send again
        assert not publisher.healthy()
        assert publisher.publish("t", "k", b"v") == (0, 0)
        assert len(FakeProducer.made) == 2 and FakeProducer.made[1].sent == [b"v"]
        assert publisher.healthy()
    finally:
        publisher.close()
