"""The fake provider's own records: a SQLite file on the provider's own volume.

The provider is a simulator of a Stripe-like payment company (synthetic data,
no real money). Its records are the source of truth the money check reconciles
our ledger against, so they live apart from our PostgreSQL. If our database
breaks, the provider's books must not break with it.

Transactional outbox: creating a payment writes the payment, its event and the
event's pending webhook delivery in one SQLite transaction. The provider can
never record a charge and forget to send its webhook, or the other way round.

Idempotency keys work like Stripe's: the same key with the same request returns
the first response and creates nothing; the same key with a different request
is refused. A request that fails validation saves nothing under its key.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED, Event

SUPPORTED_CURRENCY = "usd"

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    seq          INTEGER PRIMARY KEY,
    id           TEXT NOT NULL UNIQUE,
    customer     TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency     TEXT NOT NULL,
    metadata     TEXT NOT NULL,
    created      INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS refunds (
    seq          INTEGER PRIMARY KEY,
    id           TEXT NOT NULL UNIQUE,
    payment_id   TEXT NOT NULL REFERENCES payments(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    created      INTEGER NOT NULL
) STRICT;

-- body holds the exact bytes every delivery of the event sends and signs.
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY,
    id           TEXT NOT NULL UNIQUE,
    type         TEXT NOT NULL,
    payment_id   TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    created      INTEGER NOT NULL,
    body         BLOB NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    endpoint     TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response     TEXT NOT NULL,
    created      INTEGER NOT NULL
) STRICT;

-- The outbox: one row per event, until our endpoint acknowledges it.
CREATE TABLE IF NOT EXISTS deliveries (
    event_id        TEXT PRIMARY KEY REFERENCES events(id),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    delivered_at    REAL,
    abandoned_at    REAL
) STRICT;

-- status is NULL when no HTTP answer came back (timeout, connection refused).
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id       INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    attempt  INTEGER NOT NULL,
    sent_at  REAL NOT NULL,
    status   INTEGER,
    error    TEXT,
    ack_lost INTEGER NOT NULL CHECK (ack_lost IN (0, 1))
) STRICT;
"""


class ProviderError(Exception):
    """A request the provider refuses. The HTTP layer turns it into an error response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DueDelivery:
    event_id: str
    body: bytes
    attempts: int  # attempts made so far
    created: int  # when the event happened, unix seconds


def _canonical(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _require_cents(amount_cents: object) -> None:
    # bool is a subclass of int; True is not one cent.
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise ProviderError(400, "invalid_amount", "amount_cents must be a positive whole number of cents")


class ProviderStore:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.RLock()  # the HTTP threads and the webhook sender share one connection
        self.conn = sqlite3.connect(path, autocommit=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # Payments and refunds ------------------------------------------------

    def create_payment(
        self,
        customer: str,
        amount_cents: int,
        currency: str = SUPPORTED_CURRENCY,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict, bool]:
        """Create and capture a payment. Returns (payment, replayed)."""
        metadata = metadata or {}
        request = {"customer": customer, "amount_cents": amount_cents, "currency": currency, "metadata": metadata}

        def create() -> dict:
            _require_cents(amount_cents)
            if currency != SUPPORTED_CURRENCY:
                raise ProviderError(400, "unsupported_currency", f"this provider only handles {SUPPORTED_CURRENCY}")
            created = int(self.clock())
            payment_id = f"pay_{self._next_seq('payments'):06d}"
            self.conn.execute(
                "INSERT INTO payments (id, customer, amount_cents, currency, metadata, created)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (payment_id, customer, amount_cents, currency, _canonical(metadata), created),
            )
            self._add_event(PAYMENT_SUCCEEDED, payment_id, amount_cents, created, metadata)
            return self._payment(payment_id)

        return self._idempotent("POST /v1/payments", idempotency_key, request, create)

    def create_refund(
        self, payment_id: str, amount_cents: int, idempotency_key: str | None = None
    ) -> tuple[dict, bool]:
        """Refund part or all of a payment. Returns (refund, replayed)."""
        request = {"payment_id": payment_id, "amount_cents": amount_cents}

        def create() -> dict:
            _require_cents(amount_cents)
            payment = self.conn.execute("SELECT amount_cents FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if payment is None:
                raise ProviderError(404, "no_such_payment", f"no payment {payment_id}")
            (refunded,) = self.conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM refunds WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            if refunded + amount_cents > payment["amount_cents"]:
                raise ProviderError(400, "refund_exceeds_payment", f"refund would exceed what {payment_id} captured")
            created = int(self.clock())
            refund_id = f"re_{self._next_seq('refunds'):06d}"
            self.conn.execute(
                "INSERT INTO refunds (id, payment_id, amount_cents, created) VALUES (?, ?, ?, ?)",
                (refund_id, payment_id, amount_cents, created),
            )
            self._add_event(REFUND_SUCCEEDED, payment_id, amount_cents, created)
            return {
                "id": refund_id,
                "object": "refund",
                "status": "succeeded",
                "payment_id": payment_id,
                "amount_cents": amount_cents,
                "currency": SUPPORTED_CURRENCY,
                "created": created,
            }

        return self._idempotent("POST /v1/refunds", idempotency_key, request, create)

    def get_payment(self, payment_id: str) -> dict:
        with self._lock:
            if self.conn.execute("SELECT 1 FROM payments WHERE id = ?", (payment_id,)).fetchone() is None:
                raise ProviderError(404, "no_such_payment", f"no payment {payment_id}")
            return self._payment(payment_id)

    def _payment(self, payment_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        (refunded,) = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM refunds WHERE payment_id = ?", (payment_id,)
        ).fetchone()
        return {
            "id": row["id"],
            "object": "payment",
            "status": "succeeded",
            "customer": row["customer"],
            "amount_cents": row["amount_cents"],
            "amount_refunded_cents": refunded,
            "currency": row["currency"],
            "metadata": json.loads(row["metadata"]),
            "created": row["created"],
        }

    def _idempotent(
        self, endpoint: str, key: str | None, request: dict, create: Callable[[], dict]
    ) -> tuple[dict, bool]:
        request_hash = hashlib.sha256(_canonical(request).encode()).hexdigest()
        with self._atomic():
            if key is not None:
                seen = self.conn.execute(
                    "SELECT endpoint, request_hash, response FROM idempotency_keys WHERE key = ?", (key,)
                ).fetchone()
                if seen is not None:
                    if seen["endpoint"] != endpoint or seen["request_hash"] != request_hash:
                        raise ProviderError(
                            400, "idempotency_key_reused", "this idempotency key was used with a different request"
                        )
                    return json.loads(seen["response"]), True
            response = create()
            if key is not None:
                self.conn.execute(
                    "INSERT INTO idempotency_keys (key, endpoint, request_hash, response, created)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (key, endpoint, request_hash, _canonical(response), int(self.clock())),
                )
            return response, False

    def _next_seq(self, table: str) -> int:
        # Safe without races: callers hold the write lock (BEGIN IMMEDIATE). table is one of our own names.
        (seq,) = self.conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}").fetchone()
        return seq

    def _add_event(
        self, event_type: str, payment_id: str, amount_cents: int, created: int, metadata: dict | None = None
    ) -> str:
        event_id = f"evt_{self._next_seq('events'):06d}"
        # The first piece's payload shape. A payment's event also carries the metadata it was created with,
        # as Stripe's do: ours holds our own payment id, so our side can match the event to its payment.
        payload = Event(event_id, event_type, payment_id, amount_cents, created).payload()
        if metadata is not None:
            payload["data"]["metadata"] = metadata
        body = _canonical(payload).encode()
        self.conn.execute(
            "INSERT INTO events (id, type, payment_id, amount_cents, created, body) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, event_type, payment_id, amount_cents, created, body),
        )
        self.conn.execute(
            "INSERT INTO deliveries (event_id, attempts, next_attempt_at) VALUES (?, 0, ?)", (event_id, self.clock())
        )
        return event_id

    # Records the money check reads ------------------------------------------

    def list_events(self, limit: int = 100, starting_after: str | None = None) -> tuple[list[dict], bool]:
        """Events oldest first (Stripe lists newest first; oldest first is simpler to page through)."""
        with self._lock:
            after_seq = 0
            if starting_after is not None:
                row = self.conn.execute("SELECT seq FROM events WHERE id = ?", (starting_after,)).fetchone()
                if row is None:
                    raise ProviderError(400, "no_such_event", f"no event {starting_after}")
                after_seq = row["seq"]
            rows = self.conn.execute(
                "SELECT body FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after_seq, limit + 1)
            ).fetchall()
        return [json.loads(row["body"]) for row in rows[:limit]], len(rows) > limit

    def list_payments(self, limit: int = 100, starting_after: str | None = None) -> tuple[list[dict], bool]:
        """Payments oldest first, paged like the events."""
        with self._lock:
            after_seq = 0
            if starting_after is not None:
                row = self.conn.execute("SELECT seq FROM payments WHERE id = ?", (starting_after,)).fetchone()
                if row is None:
                    raise ProviderError(400, "no_such_payment", f"no payment {starting_after}")
                after_seq = row["seq"]
            ids = [
                row["id"]
                for row in self.conn.execute(
                    "SELECT id FROM payments WHERE seq > ? ORDER BY seq LIMIT ?", (after_seq, limit + 1)
                )
            ]
            return [self._payment(payment_id) for payment_id in ids[:limit]], len(ids) > limit

    def balance_cents(self) -> int:
        """What the provider holds for us: captured minus refunded."""
        with self._lock:
            (captured,) = self.conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM payments").fetchone()
            (refunded,) = self.conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM refunds").fetchone()
        return captured - refunded

    # The webhook outbox ---------------------------------------------------

    def due_deliveries(self, now: float, limit: int = 50) -> list[DueDelivery]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT d.event_id, e.body, d.attempts, e.created FROM deliveries d JOIN events e ON e.id = d.event_id"
                " WHERE d.delivered_at IS NULL AND d.abandoned_at IS NULL AND d.next_attempt_at <= ?"
                " ORDER BY d.next_attempt_at, e.seq LIMIT ?",
                (now, limit),
            ).fetchall()
        return [DueDelivery(r["event_id"], bytes(r["body"]), r["attempts"], r["created"]) for r in rows]

    def next_due_at(self) -> float | None:
        with self._lock:
            (due,) = self.conn.execute(
                "SELECT MIN(next_attempt_at) FROM deliveries WHERE delivered_at IS NULL AND abandoned_at IS NULL"
            ).fetchone()
        return due

    def record_attempt(
        self,
        event_id: str,
        attempt: int,
        sent_at: float,
        status: int | None,
        error: str | None,
        ack_lost: bool,
        outcome: str,
        next_attempt_at: float,
    ) -> None:
        """outcome: "delivered" (acknowledged), "retry" (try again at next_attempt_at) or "abandoned"."""
        if outcome not in ("delivered", "retry", "abandoned"):
            raise ValueError(f"unknown outcome {outcome!r}")
        with self._atomic():
            self.conn.execute(
                "INSERT INTO delivery_attempts (event_id, attempt, sent_at, status, error, ack_lost)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, attempt, sent_at, status, error, int(ack_lost)),
            )
            self.conn.execute(
                "UPDATE deliveries SET attempts = ?, next_attempt_at = ?,"
                " delivered_at = CASE WHEN ? = 'delivered' THEN ? END,"
                " abandoned_at = CASE WHEN ? = 'abandoned' THEN ? END"
                " WHERE event_id = ?",
                (attempt, next_attempt_at, outcome, sent_at, outcome, sent_at, event_id),
            )

    def delivery_stats(self) -> dict:
        with self._lock:
            (events,) = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()
            pending, delivered, abandoned = self.conn.execute(
                "SELECT COALESCE(SUM(delivered_at IS NULL AND abandoned_at IS NULL), 0),"
                "       COALESCE(SUM(delivered_at IS NOT NULL), 0), COALESCE(SUM(abandoned_at IS NOT NULL), 0)"
                " FROM deliveries"
            ).fetchone()
            attempts, answered_2xx, answered_4xx, answered_5xx, no_answer, lost_acks = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(status BETWEEN 200 AND 299), 0),"
                "       COALESCE(SUM(status BETWEEN 400 AND 499), 0), COALESCE(SUM(status >= 500), 0),"
                "       COALESCE(SUM(status IS NULL), 0), COALESCE(SUM(ack_lost), 0)"
                " FROM delivery_attempts"
            ).fetchone()
            # A duplicate is a delivery our endpoint accepted (2xx) for an event it had already accepted.
            (duplicates,) = self.conn.execute(
                "SELECT COALESCE(SUM(n - 1), 0) FROM (SELECT COUNT(*) AS n FROM delivery_attempts"
                " WHERE status BETWEEN 200 AND 299 GROUP BY event_id) WHERE n > 1"
            ).fetchone()
        return {
            "events": events,
            "pending": pending,
            "delivered": delivered,
            "abandoned": abandoned,
            "attempts": attempts,
            "answered_2xx": answered_2xx,
            "answered_4xx": answered_4xx,
            "answered_5xx": answered_5xx,
            "no_answer": no_answer,
            "acks_lost_on_purpose": lost_acks,
            "duplicate_deliveries": duplicates,
        }

    def delivery_attempts(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, event_id, attempt, sent_at, status, error, ack_lost FROM delivery_attempts ORDER BY id"
            ).fetchall()
        return [dict(row) | {"ack_lost": bool(row["ack_lost"])} for row in rows]
