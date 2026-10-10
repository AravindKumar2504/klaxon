"""The live ledger: the first piece's double-entry books, in PostgreSQL.

Same accounts, same posting rules (`klaxon.patient.ledger.posting_lines` and
`check_lines`), and the same table and column names as the first piece's
SQLite ledger, so the money check runs unchanged on both. The schema is in
docker/postgres/initdb/20-schema.sql.

The consumer connects as ledger_writer, a role that may SELECT and INSERT
ledger rows but has no UPDATE or DELETE. "Ledger rows are never edited or
deleted; a wrong posting is repaired by a reversal" is enforced by the
database, not only by our code.

Modes (the first piece's two handlers):
- "idempotent": insert the event id into processed_events (primary key) in the
  same transaction as the posting. A repeated event writes nothing. If the
  posting fails, the dedupe row rolls back with it, so a retry can still post.
- "naive": post every message. The known-bad baseline the money check must catch.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from klaxon.patient.ledger import check_lines, posting_lines
from klaxon.patient.services.events import ProviderEvent

MODES = ("idempotent", "naive")


class PgLedger:
    def __init__(self, pool: ConnectionPool, mode: str = "idempotent") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        self.pool = pool  # connections in autocommit mode; transaction() blocks make the transactions
        self.mode = mode

    def book(self, event: ProviderEvent, source: str) -> str:
        """Book one provider event. Returns "posted", "already_processed" or "ignored_type".

        source says where the event came from (topic/partition@offset), kept on
        the transaction so an investigation can trace a posting to its message.
        """
        lines = posting_lines(event.type, event.amount_cents)
        if lines is None:
            return "ignored_type"
        check_lines(event.id, lines)
        with self.pool.connection() as conn, conn.transaction():
            if self.mode == "idempotent":
                inserted = conn.execute(
                    "INSERT INTO processed_events (event_id) VALUES (%s) ON CONFLICT (event_id) DO NOTHING",
                    (event.id,),
                ).rowcount
                if inserted == 0:
                    return "already_processed"  # seen before; the block commits nothing new
            (transaction_id,) = conn.execute(
                "INSERT INTO transactions (event_id, payment_id, description, source)"
                " VALUES (%s, %s, %s, %s) RETURNING id",
                (event.id, event.payment_id, f"{event.type} {event.payment_id}", source),
            ).fetchone()
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO entries (transaction_id, account, side, amount_cents) VALUES (%s, %s, %s, %s)",
                    [(transaction_id, line.account, line.side, line.amount_cents) for line in lines],
                )
        return "posted"
