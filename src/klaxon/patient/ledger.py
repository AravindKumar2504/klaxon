"""A small double-entry ledger (SQLite in memory) and our webhook endpoint.

Double-entry: every transaction has debit lines and credit lines that sum to
the same amount, so total debits always equal total credits. Money is stored as
integer cents in STRICT tables; no floats.

Accounts:
- provider_clearing (debit-normal asset): money the provider holds for us.
- sales (credit-normal revenue).
- refunds (debit-normal contra-revenue).

Ledger rows are never edited or deleted. A wrong posting is repaired by a
reversal: a new transaction with every line flipped, pointing at the original.

The endpoint has two modes:
- "naive" posts every delivery it receives. That is only correct if every
  event arrives exactly once, which no webhook provider promises.
- "idempotent" records the event id in processed_events (primary key) inside
  the same database transaction as the posting. A repeated event gets a 200
  and posts nothing.

The trap this module exists to show: a duplicate posting is itself balanced,
so "debits equal credits" still holds after a payment is counted twice.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from klaxon.patient import signatures
from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED, WebhookRequest

ACCOUNTS = {"provider_clearing": "debit", "sales": "credit", "refunds": "debit"}
SIDES = ("debit", "credit")
MODES = ("naive", "idempotent")
CURRENCY = "usd"  # the ledger is single-currency; anything else is refused

SCHEMA = """
CREATE TABLE accounts (
    name        TEXT PRIMARY KEY,
    normal_side TEXT NOT NULL CHECK (normal_side IN ('debit', 'credit'))
) STRICT;

-- event_id is deliberately NOT unique here: the ledger records what the
-- handler tells it to, which is how the naive handler can post twice.
CREATE TABLE transactions (
    id                      INTEGER PRIMARY KEY,
    event_id                TEXT NOT NULL,
    payment_id              TEXT NOT NULL,
    description             TEXT NOT NULL,
    posted_at               INTEGER NOT NULL,
    reverses_transaction_id INTEGER UNIQUE REFERENCES transactions(id)  -- at most one reversal each
) STRICT;

CREATE TABLE entries (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    account        TEXT NOT NULL REFERENCES accounts(name),
    side           TEXT NOT NULL CHECK (side IN ('debit', 'credit')),
    amount_cents   INTEGER NOT NULL CHECK (amount_cents > 0)
) STRICT;

-- Written only by the idempotent handler. The primary key is the dedupe.
CREATE TABLE processed_events (
    event_id     TEXT PRIMARY KEY,
    processed_at INTEGER NOT NULL
) STRICT;
"""


class UnbalancedTransaction(ValueError):
    pass


@dataclass(frozen=True)
class Line:
    account: str
    side: str  # "debit" or "credit"
    amount_cents: int


def posting_lines(event_type: str, amount_cents: int) -> list[Line] | None:
    """How each provider event is booked. None means we do not book that event type."""
    if event_type == PAYMENT_SUCCEEDED:
        return [Line("provider_clearing", "debit", amount_cents), Line("sales", "credit", amount_cents)]
    if event_type == REFUND_SUCCEEDED:
        return [Line("refunds", "debit", amount_cents), Line("provider_clearing", "credit", amount_cents)]
    return None


def _is_cents(value: object) -> bool:
    # bool is a subclass of int; True is not one cent.
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def check_lines(event_id: str, lines: list[Line]) -> None:
    """Refuse bad lines: unknown account or side, non-cents amounts, fewer than two lines, or unbalanced."""
    for line in lines:
        if line.account not in ACCOUNTS or line.side not in SIDES or not _is_cents(line.amount_cents):
            raise ValueError(f"bad line for {event_id}: {line}")
    debits = sum(line.amount_cents for line in lines if line.side == "debit")
    credits = sum(line.amount_cents for line in lines if line.side == "credit")
    if len(lines) < 2 or debits != credits:
        raise UnbalancedTransaction(f"debits {debits} != credits {credits} for {event_id}")


class Ledger:
    def __init__(self) -> None:
        # autocommit=True: the sqlite3 module never opens a transaction behind
        # our back; _atomic() below opens and closes every one explicitly.
        self.conn = sqlite3.connect(":memory:", autocommit=True)
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.conn.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise RuntimeError("SQLite foreign keys could not be enabled")
        self.conn.executescript(SCHEMA)
        with self._atomic():
            self.conn.executemany("INSERT INTO accounts (name, normal_side) VALUES (?, ?)", ACCOUNTS.items())

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """One database transaction: all of it or none of it."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def post(
        self,
        *,
        event_id: str,
        payment_id: str,
        description: str,
        lines: list[Line],
        posted_at: int,
        dedupe: bool = False,
    ) -> bool:
        """Write one balanced transaction. Return False if dedupe found the event already posted."""
        check_lines(event_id, lines)
        with self._atomic():
            if dedupe:
                inserted = self.conn.execute(
                    "INSERT INTO processed_events (event_id, processed_at) VALUES (?, ?)"
                    " ON CONFLICT (event_id) DO NOTHING",
                    (event_id, posted_at),
                ).rowcount
                if inserted == 0:
                    return False  # seen before; nothing is written
            self._insert(event_id, payment_id, description, lines, posted_at)
        return True

    def reverse(self, transaction_id: int, *, reason: str, posted_at: int) -> int:
        """Cancel a posting by adding its mirror image. Returns the new transaction id."""
        with self._atomic():
            original = self.conn.execute(
                "SELECT event_id, payment_id, reverses_transaction_id FROM transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            if original is None:
                raise ValueError(f"no transaction {transaction_id}")
            event_id, payment_id, reverses = original
            if reverses is not None:
                raise ValueError(f"transaction {transaction_id} is itself a reversal")
            flipped = [
                Line(account, "credit" if side == "debit" else "debit", amount)
                for account, side, amount in self.conn.execute(
                    "SELECT account, side, amount_cents FROM entries WHERE transaction_id = ? ORDER BY id",
                    (transaction_id,),
                )
            ]
            check_lines(event_id, flipped)
            # The UNIQUE constraint on reverses_transaction_id refuses a second reversal.
            return self._insert(
                event_id, payment_id, f"reversal of #{transaction_id}: {reason}", flipped, posted_at, transaction_id
            )

    def _insert(
        self,
        event_id: str,
        payment_id: str,
        description: str,
        lines: list[Line],
        posted_at: int,
        reverses: int | None = None,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO transactions (event_id, payment_id, description, posted_at, reverses_transaction_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_id, payment_id, description, posted_at, reverses),
        )
        self.conn.executemany(
            "INSERT INTO entries (transaction_id, account, side, amount_cents) VALUES (?, ?, ?, ?)",
            [(cursor.lastrowid, line.account, line.side, line.amount_cents) for line in lines],
        )
        return cursor.lastrowid

    def balance_cents(self, account: str) -> int:
        """Balance on the account's normal side (positive is the usual direction)."""
        (net_debit,) = self.conn.execute(
            "SELECT COALESCE(SUM(CASE side WHEN 'debit' THEN amount_cents ELSE -amount_cents END), 0)"
            " FROM entries WHERE account = ?",
            (account,),
        ).fetchone()
        return net_debit if ACCOUNTS[account] == "debit" else -net_debit

    def transactions(self) -> list[dict]:
        out: dict[int, dict] = {}
        for tx_id, event_id, payment_id, description, reverses in self.conn.execute(
            "SELECT id, event_id, payment_id, description, reverses_transaction_id FROM transactions ORDER BY id"
        ):
            out[tx_id] = {
                "id": tx_id,
                "event_id": event_id,
                "payment_id": payment_id,
                "description": description,
                "reverses_transaction_id": reverses,
                "lines": [],
            }
        for tx_id, account, side, amount in self.conn.execute(
            "SELECT transaction_id, account, side, amount_cents FROM entries ORDER BY id"
        ):
            out[tx_id]["lines"].append({"account": account, "side": side, "amount_cents": amount})
        return list(out.values())


@dataclass(frozen=True)
class RequestRecord:
    """What our HTTP layer knows about one request. This is all a dashboard sees."""

    event_id: str | None
    status: int
    outcome: str  # posted, already_processed, bad_signature, bad_payload, ignored_type, error
    error: str | None = None  # the exception behind a 500, kept for whoever investigates


class WebhookEndpoint:
    def __init__(
        self,
        ledger: Ledger,
        secret: bytes,
        mode: str,
        now: Callable[[], int],
        tolerance_seconds: int = signatures.DEFAULT_TOLERANCE_SECONDS,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        self.ledger = ledger
        self.secret = secret
        self.mode = mode
        self.now = now
        self.tolerance_seconds = tolerance_seconds
        self.requests: list[RequestRecord] = []

    def __call__(self, request: WebhookRequest) -> int:
        record = self._handle(request)
        self.requests.append(record)
        return record.status

    def _handle(self, request: WebhookRequest) -> RequestRecord:
        headers = {name.lower(): value for name, value in request.headers.items()}  # HTTP headers ignore case
        try:
            signatures.verify(
                headers.get(signatures.HEADER.lower()), request.body, self.secret, self.now(), self.tolerance_seconds
            )
        except signatures.SignatureError:
            return RequestRecord(None, 400, "bad_signature")

        try:
            event = json.loads(request.body)
            event_id, event_type, data = event["id"], event["type"], event["data"]
            payment_id, amount = data["payment_id"], data["amount_cents"]
            if not all(isinstance(v, str) for v in (event_id, event_type, payment_id)):
                raise TypeError("ids must be strings")
            if not _is_cents(amount):
                raise TypeError("amount must be positive integer cents")
            if data.get("currency") != CURRENCY:
                raise ValueError("unsupported currency")
        except (ValueError, KeyError, TypeError):
            return RequestRecord(None, 400, "bad_payload")

        lines = posting_lines(event_type, amount)
        if lines is None:
            return RequestRecord(event_id, 200, "ignored_type")  # acknowledge, or the provider keeps retrying

        try:
            posted = self.ledger.post(
                event_id=event_id,
                payment_id=payment_id,
                description=f"{event_type} {payment_id}",
                lines=lines,
                posted_at=self.now(),
                dedupe=self.mode == "idempotent",
            )
        except Exception as exc:  # what a web framework does with an unhandled error
            return RequestRecord(event_id, 500, "error", repr(exc))
        return RequestRecord(event_id, 200, "posted" if posted else "already_processed")
