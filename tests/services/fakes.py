"""In-memory stand-ins for Redis, PostgreSQL, the provider and Kafka, so the service tests run offline.

The real things are tested in tests/integration (PostgreSQL, Kafka) and tests/e2e (the whole stack).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import redis

from klaxon.patient.services import idempotency
from klaxon.patient.services.payments_store import PaymentRow, RefundRow
from klaxon.patient.services.provider_client import ProviderRejected, ProviderUnavailable


class FakeRedis:
    """The commands RedisIdempotency uses, including its two Lua scripts. down=True fails like a stopped server."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise redis.ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    def set(self, name: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self._check()
        if nx and name in self.data:
            return None
        self.data[name] = value
        if ex is not None:
            self.ttls[name] = ex
        return True

    def get(self, name: str) -> bytes | None:
        self._check()
        value = self.data.get(name)
        return value.encode() if value is not None else None

    def delete(self, name: str) -> int:
        self._check()
        self.ttls.pop(name, None)
        return 1 if self.data.pop(name, None) is not None else 0

    def ping(self) -> bool:
        self._check()
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args):
        """Runs RedisIdempotency's RELEASE and COMPLETE scripts with the same logic, in Python."""
        self._check()
        name, args = keys_and_args[0], keys_and_args[numkeys:]
        current = self.data.get(name)
        if script == idempotency.RELEASE:
            if current == args[0]:
                return self.delete(name)
            return 0
        if script == idempotency.COMPLETE:
            if current is None or current == args[0]:
                return self.set(name, args[1], ex=int(args[2])) and 1
            return 0
        raise NotImplementedError("unknown script")

    def expire_now(self, name: str) -> None:
        """What Redis does when a key's time to live runs out."""
        self.data.pop(name, None)
        self.ttls.pop(name, None)

    def record(self, name: str) -> dict | None:
        value = self.data.get(name)
        return json.loads(value) if value is not None else None


class MemoryPaymentsStore:
    """PaymentsStore with the same rules as the PostgreSQL one: UNIQUE idempotency keys, ids in order."""

    def __init__(self) -> None:
        self.payments: dict[str, PaymentRow] = {}
        self.refunds: dict[str, RefundRow] = {}
        self.down = False
        self.clock = self._now()  # "now", for pending_counts; every row is created at _now()

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("database unreachable")

    @staticmethod
    def _now() -> datetime:
        return datetime(2026, 10, 9, tzinfo=timezone.utc)

    def insert_or_get_payment(self, idempotency_key, fingerprint, customer, amount_cents, currency):
        self._check()
        for row in self.payments.values():
            if row.idempotency_key == idempotency_key:
                return row, False
        row = PaymentRow(
            id=f"pmt_{len(self.payments) + 1:06d}",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            customer=customer,
            amount_cents=amount_cents,
            currency=currency,
            status="pending",
            provider_payment_id=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        self.payments[row.id] = row
        return row, True

    def mark_payment(self, payment_id, status, provider_payment_id):
        self._check()
        row = self.payments[payment_id]
        row = replace(row, status=status, provider_payment_id=provider_payment_id or row.provider_payment_id)
        self.payments[payment_id] = row
        return row

    def get_payment(self, payment_id):
        self._check()
        return self.payments.get(payment_id)

    def insert_or_get_refund(self, idempotency_key, fingerprint, payment_id, amount_cents):
        self._check()
        for row in self.refunds.values():
            if row.idempotency_key == idempotency_key:
                return row, False
        row = RefundRow(
            id=f"rfd_{len(self.refunds) + 1:06d}",
            payment_id=payment_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            amount_cents=amount_cents,
            status="pending",
            provider_refund_id=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        self.refunds[row.id] = row
        return row, True

    def mark_refund(self, refund_id, status, provider_refund_id):
        self._check()
        row = self.refunds[refund_id]
        row = replace(row, status=status, provider_refund_id=provider_refund_id or row.provider_refund_id)
        self.refunds[refund_id] = row
        return row

    def refunds_for(self, payment_id):
        self._check()
        return [row for row in self.refunds.values() if row.payment_id == payment_id]

    def pending_counts(self, older_than_seconds):
        self._check()
        pending = [row for row in self.payments.values() if row.status == "pending"]
        cutoff = self.clock - timedelta(seconds=older_than_seconds)
        return len(pending), sum(1 for row in pending if row.created_at < cutoff)

    def ping(self) -> None:
        self._check()


class FakeProvider:
    """A ProviderClient stand-in that, like the real provider, answers a repeated key with the first result."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []  # (call, idempotency_key, body)
        self.results: dict[str, dict] = {}
        self.charged: list[str] = []  # payment ids actually created
        self.unavailable = False
        self.reject: tuple[int, str, str] | None = None

    def create_payment(self, customer, amount_cents, currency, idempotency_key, metadata):
        body = {"customer": customer, "amount_cents": amount_cents, "currency": currency, "metadata": metadata}
        self.calls.append(("create_payment", idempotency_key, body))
        return self._answer(idempotency_key, lambda: self._charge(body))

    def create_refund(self, payment_id, amount_cents, idempotency_key):
        body = {"payment_id": payment_id, "amount_cents": amount_cents}
        self.calls.append(("create_refund", idempotency_key, body))
        return self._answer(idempotency_key, lambda: {"id": f"re_{len(self.results) + 1:06d}", **body})

    def _charge(self, body: dict) -> dict:
        payment = {"id": f"pay_{len(self.charged) + 1:06d}", "status": "succeeded", **body}
        self.charged.append(payment["id"])
        return payment

    def _answer(self, key, create):
        if self.unavailable:
            raise ProviderUnavailable("timed out after 3 attempts")
        if self.reject is not None:
            raise ProviderRejected(*self.reject)
        if key not in self.results:
            self.results[key] = create()
        return self.results[key]


class FakeMessage:
    """Enough of confluent_kafka.Message for the ledger consumer."""

    def __init__(
        self,
        value: bytes,
        offset: int,
        partition: int = 0,
        key: bytes | None = b"pay_000001",
        error=None,
        headers: list[tuple[str, bytes]] | None = None,
    ):
        self._value, self._offset, self._partition, self._key, self._error = value, offset, partition, key, error
        self._headers = headers

    def value(self) -> bytes:
        return self._value

    def key(self) -> bytes | None:
        return self._key

    def topic(self) -> str:
        return "provider-events"

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def error(self):
        return self._error

    def headers(self) -> list[tuple[str, bytes]] | None:
        return self._headers  # like confluent_kafka: None when the message has no headers


class FakeConsumer:
    """Hands out queued messages, records commits, and sets stop when it runs dry."""

    def __init__(self, messages: list[FakeMessage], stop, log: list[str] | None = None) -> None:
        self.messages = list(messages)
        self.stop = stop
        self.log = log if log is not None else []
        self.committed: list[int] = []
        self.commit_error: Exception | None = None

    def poll(self, timeout: float):
        if not self.messages:
            self.stop.set()
            return None
        return self.messages.pop(0)

    def commit(self, message, asynchronous: bool = True) -> None:
        assert asynchronous is False, "the ledger consumer must commit synchronously, after booking"
        if self.commit_error is not None:
            raise self.commit_error
        self.log.append(f"commit {message.offset()}")
        self.committed.append(message.offset())


class FakePublisher:
    """Records what would have gone to Kafka. fail=True makes publish raise like a broker outage."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.messages: list[dict] = []
        self.fail = fail
        self.is_healthy = True

    def publish(self, topic, key, value, headers=None):
        if self.fail is not None:
            raise self.fail
        self.messages.append({"topic": topic, "key": key, "value": value, "headers": dict(headers or {})})
        return 0, len(self.messages) - 1

    def healthy(self) -> bool:
        return self.is_healthy
