"""Our own payment records in PostgreSQL (schema "payments"), used by the payments API.

payments.idempotency_key is UNIQUE: the database's guarantee behind Redis.
The API connects as payments_api, a role that can read and write these two
tables and cannot touch the ledger. The ledger is booked only from the
provider's webhooks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from psycopg_pool import ConnectionPool

STATUSES = ("pending", "succeeded", "failed")


@dataclass(frozen=True)
class PaymentRow:
    id: str
    idempotency_key: str
    request_fingerprint: str
    customer: str
    amount_cents: int
    currency: str
    status: str
    provider_payment_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RefundRow:
    id: str
    payment_id: str
    idempotency_key: str
    request_fingerprint: str
    amount_cents: int
    status: str
    provider_refund_id: str | None
    created_at: datetime
    updated_at: datetime


class PaymentsStore(Protocol):
    def insert_or_get_payment(
        self, idempotency_key: str, fingerprint: str, customer: str, amount_cents: int, currency: str
    ) -> tuple[PaymentRow, bool]: ...

    def mark_payment(self, payment_id: str, status: str, provider_payment_id: str | None) -> PaymentRow: ...

    def get_payment(self, payment_id: str) -> PaymentRow | None: ...

    def insert_or_get_refund(
        self, idempotency_key: str, fingerprint: str, payment_id: str, amount_cents: int
    ) -> tuple[RefundRow, bool]: ...

    def mark_refund(self, refund_id: str, status: str, provider_refund_id: str | None) -> RefundRow: ...

    def refunds_for(self, payment_id: str) -> list[RefundRow]: ...

    def ping(self) -> None: ...


class PgPaymentsStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool  # autocommit connections with dict rows

    def insert_or_get_payment(
        self, idempotency_key: str, fingerprint: str, customer: str, amount_cents: int, currency: str
    ) -> tuple[PaymentRow, bool]:
        """(row, created). A repeated key returns the row that already exists, whatever Redis knows."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO payments (idempotency_key, request_fingerprint, customer, amount_cents, currency, status)"
                " VALUES (%s, %s, %s, %s, %s, 'pending') ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                (idempotency_key, fingerprint, customer, amount_cents, currency),
            ).fetchone()
            if row is not None:
                return PaymentRow(**row), True
            row = conn.execute("SELECT * FROM payments WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
            return PaymentRow(**row), False

    def mark_payment(self, payment_id: str, status: str, provider_payment_id: str | None) -> PaymentRow:
        with self.pool.connection() as conn:
            row = conn.execute(
                "UPDATE payments SET status = %s, provider_payment_id = COALESCE(%s, provider_payment_id),"
                " updated_at = now() WHERE id = %s RETURNING *",
                (status, provider_payment_id, payment_id),
            ).fetchone()
        return PaymentRow(**row)

    def get_payment(self, payment_id: str) -> PaymentRow | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM payments WHERE id = %s", (payment_id,)).fetchone()
        return PaymentRow(**row) if row else None

    def insert_or_get_refund(
        self, idempotency_key: str, fingerprint: str, payment_id: str, amount_cents: int
    ) -> tuple[RefundRow, bool]:
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO refunds (idempotency_key, request_fingerprint, payment_id, amount_cents, status)"
                " VALUES (%s, %s, %s, %s, 'pending') ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                (idempotency_key, fingerprint, payment_id, amount_cents),
            ).fetchone()
            if row is not None:
                return RefundRow(**row), True
            row = conn.execute("SELECT * FROM refunds WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
            return RefundRow(**row), False

    def mark_refund(self, refund_id: str, status: str, provider_refund_id: str | None) -> RefundRow:
        with self.pool.connection() as conn:
            row = conn.execute(
                "UPDATE refunds SET status = %s, provider_refund_id = COALESCE(%s, provider_refund_id),"
                " updated_at = now() WHERE id = %s RETURNING *",
                (status, provider_refund_id, refund_id),
            ).fetchone()
        return RefundRow(**row)

    def refunds_for(self, payment_id: str) -> list[RefundRow]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM refunds WHERE payment_id = %s ORDER BY id", (payment_id,)).fetchall()
        return [RefundRow(**row) for row in rows]

    def ping(self) -> None:
        with self.pool.connection(timeout=2.0) as conn:
            conn.execute("SELECT 1")
