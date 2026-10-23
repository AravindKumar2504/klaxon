"""Our payment records' store, offline: how it takes its database connections (week 4, item 4, fault 6).

The pool here is made up: it hands out made-up connections and counts those not given back. The real pool and
PostgreSQL are tested in tests/integration/test_postgres.py.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

from klaxon.patient.services.payments_store import PgPaymentsStore

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
REFUND = {
    "id": "rfd_000001",
    "payment_id": "pmt_000001",
    "idempotency_key": "r1",
    "request_fingerprint": "fp",
    "amount_cents": 1_000,
    "status": "succeeded",
    "provider_refund_id": "re_000001",
    "created_at": NOW,
    "updated_at": NOW,
}


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class MadeUpConnection:
    def __init__(self, asked):
        self.asked = asked

    def execute(self, query, params):
        self.asked.append((query, params))
        return Rows([REFUND])


class MadeUpPool:
    """psycopg_pool's two ways to take a connection: connection() gives it back at the end of its block; getconn()
    leaves that to putconn()."""

    def __init__(self):
        self.asked: list = []
        self.out = 0  # connections taken and not given back

    @contextmanager
    def connection(self, timeout=None):
        self.out += 1
        try:
            yield MadeUpConnection(self.asked)
        finally:
            self.out -= 1

    def getconn(self, timeout=None):
        self.out += 1
        return MadeUpConnection(self.asked)

    def putconn(self, conn):
        self.out -= 1


def test_reading_a_payments_refunds_gives_its_connection_back():
    pool = MadeUpPool()
    store = PgPaymentsStore(pool)
    for _ in range(3):
        assert [refund.id for refund in store.refunds_for("pmt_000001")] == ["rfd_000001"]
    assert pool.out == 0 and len(pool.asked) == 3


def test_with_the_known_bad_leak_each_read_of_refunds_keeps_its_connection():
    """REFUND_CONNECTION=kept: the same rows, read the same way, and one connection fewer in the pool each time."""
    pool = MadeUpPool()
    store = PgPaymentsStore(pool, keep_refund_connections=True)
    for n in range(1, 4):
        assert [refund.id for refund in store.refunds_for("pmt_000001")] == ["rfd_000001"]
        assert pool.out == n
    assert pool.asked == [("SELECT * FROM refunds WHERE payment_id = %s ORDER BY id", ("pmt_000001",))] * 3
