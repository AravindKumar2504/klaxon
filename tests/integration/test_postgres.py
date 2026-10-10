"""The live ledger and payments tables in real PostgreSQL: booking, permissions, and the money check's SQL."""

import threading

import psycopg
import pytest

from klaxon.checks.money import MONEY_EFFECT, check_money
from klaxon.patient.ledger import Line
from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED, Event
from klaxon.patient.services import pg_ledger
from klaxon.patient.services.events import ProviderEvent
from klaxon.patient.services.payments_store import PgPaymentsStore
from klaxon.patient.services.pg_ledger import PgLedger

pytestmark = pytest.mark.integration

PAYMENT = ProviderEvent("evt_000001", PAYMENT_SUCCEEDED, 1_791_504_001, "pay_000001", 2_500, "usd")
REFUND = ProviderEvent("evt_000002", REFUND_SUCCEEDED, 1_791_504_002, "pay_000001", 1_000, "usd")


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_the_idempotent_handler_books_an_event_once(pool_as, connect):
    ledger = PgLedger(pool_as("ledger_writer"), "idempotent")
    assert ledger.book(PAYMENT, "provider-events/0@0") == "posted"
    assert ledger.book(PAYMENT, "provider-events/0@1") == "already_processed"
    admin = connect("postgres")
    assert (count(admin, "ledger.transactions"), count(admin, "ledger.entries")) == (1, 2)
    assert admin.execute("SELECT source FROM ledger.transactions").fetchone() == ("provider-events/0@0",)


def test_the_naive_handler_books_it_twice_and_the_ledger_still_balances(pool_as, connect):
    ledger = PgLedger(pool_as("ledger_writer"), "naive")
    ledger.book(PAYMENT, "provider-events/0@0")
    ledger.book(PAYMENT, "provider-events/0@1")
    admin = connect("postgres")
    assert count(admin, "ledger.transactions") == 2 and count(admin, "ledger.processed_events") == 0
    debits, credits = admin.execute(
        "SELECT sum(amount_cents) FILTER (WHERE side = 'debit'), sum(amount_cents) FILTER (WHERE side = 'credit')"
        " FROM ledger.entries"
    ).fetchone()
    assert debits == credits == 5_000


def test_event_types_without_money_are_not_booked(pool_as, connect):
    other = ProviderEvent("evt_000009", "payment.created", 1, "pay_000009", 100, "usd")
    assert PgLedger(pool_as("ledger_writer")).book(other, "x") == "ignored_type"
    assert count(connect("postgres"), "ledger.transactions") == 0


def test_a_posting_that_fails_halfway_rolls_back_its_dedupe_row_too(pool_as, connect, monkeypatch):
    # Force the entries insert to fail inside the transaction (an account the database does not know).
    monkeypatch.setattr(pg_ledger, "check_lines", lambda event_id, lines: None)
    monkeypatch.setattr(
        pg_ledger,
        "posting_lines",
        lambda event_type, cents: [Line("provider_clearing", "debit", cents), Line("no_such_account", "credit", cents)],
    )
    ledger = PgLedger(pool_as("ledger_writer"), "idempotent")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        ledger.book(PAYMENT, "provider-events/0@0")
    admin = connect("postgres")
    # Nothing at all was written, so a retry after the fix can still post the event.
    assert (count(admin, "ledger.processed_events"), count(admin, "ledger.transactions")) == (0, 0)


def test_two_consumers_racing_on_one_event_book_it_once(pool_as, connect):
    pool = pool_as("ledger_writer")
    ledgers = [PgLedger(pool, "idempotent"), PgLedger(pool, "idempotent")]
    outcomes: list[str] = []
    start = threading.Barrier(2)

    def book(ledger):
        start.wait()
        outcomes.append(ledger.book(PAYMENT, "provider-events/0@0"))

    threads = [threading.Thread(target=book, args=(ledger,)) for ledger in ledgers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["already_processed", "posted"]
    assert count(connect("postgres"), "ledger.transactions") == 1


# Permissions --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE transactions SET payment_id = 'pay_x'",
        "UPDATE entries SET amount_cents = 1",
        "DELETE FROM entries",
        "DELETE FROM transactions",
        "DELETE FROM processed_events",
        "TRUNCATE entries",
        "INSERT INTO accounts VALUES ('slush_fund', 'debit')",
        "SELECT * FROM payments.payments",
    ],
)
def test_the_ledger_writer_can_add_rows_but_never_edit_or_delete_them(pool_as, connect, statement):
    PgLedger(pool_as("ledger_writer")).book(PAYMENT, "provider-events/0@0")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        connect("ledger_writer").execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO processed_events (event_id) VALUES ('evt_forged')",
        "UPDATE payments SET status = 'failed'",
        "DELETE FROM entries",
        "CREATE TABLE notes (x int)",
    ],
)
def test_klaxons_role_can_read_everything_and_write_nothing(connect, statement):
    readonly = connect("klaxon_readonly")
    assert readonly.execute("SELECT count(*) FROM transactions").fetchone() == (0,)
    assert readonly.execute("SELECT count(*) FROM payments").fetchone() == (0,)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):  # the first layer: read-only transactions
        readonly.execute(statement)
    # Even with read-only switched off for the session, the grants refuse the write: they are the real guard.
    readonly.execute("SET default_transaction_read_only = off")
    with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable)):
        readonly.execute(statement)


def test_no_role_can_create_temporary_tables(connect):
    for role in ("klaxon_readonly", "ledger_writer", "payments_api"):
        conn = connect(role)
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TEMPORARY TABLE scratch (x int)")


def test_klaxons_role_has_a_statement_timeout(connect):
    with pytest.raises(psycopg.errors.QueryCanceled):
        connect("klaxon_readonly").execute("SELECT pg_sleep(6)")


def test_the_payments_api_can_change_a_status_but_never_an_amount(pool_as, connect):
    store = PgPaymentsStore(pool_as("payments_api", dict_rows=True))
    payment, created = store.insert_or_get_payment("key-1", "fp", "cus_001", 2_500, "usd")
    assert (payment.id, payment.status, created) == ("pmt_000001", "pending", True)
    assert store.mark_payment(payment.id, "succeeded", "pay_000001").provider_payment_id == "pay_000001"
    api = connect("payments_api")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute("UPDATE payments SET amount_cents = 250000 WHERE id = 'pmt_000001'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute("SELECT * FROM ledger.entries")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute("DELETE FROM payments")


def test_a_repeated_idempotency_key_finds_the_same_payment(pool_as):
    store = PgPaymentsStore(pool_as("payments_api", dict_rows=True))
    first, created_first = store.insert_or_get_payment("key-1", "fp", "cus_001", 2_500, "usd")
    again, created_again = store.insert_or_get_payment("key-1", "fp", "cus_001", 2_500, "usd")
    assert (created_first, created_again, again.id) == (True, False, first.id)


def test_repeated_keys_do_not_use_up_payment_ids(pool_as):
    store = PgPaymentsStore(pool_as("payments_api", dict_rows=True))
    store.insert_or_get_payment("key-1", "fp", "cus_001", 2_500, "usd")
    for _ in range(3):
        store.insert_or_get_payment("key-1", "fp", "cus_001", 2_500, "usd")
    second, created = store.insert_or_get_payment("key-2", "fp", "cus_002", 900, "usd")
    assert (second.id, created) == ("pmt_000002", True)


def test_concurrent_requests_with_one_key_create_one_payment(pool_as, connect):
    store = PgPaymentsStore(pool_as("payments_api", dict_rows=True))
    created: list[bool] = []
    start = threading.Barrier(4)

    def insert():
        start.wait()
        created.append(store.insert_or_get_payment("same-key", "fp", "cus_001", 2_500, "usd")[1])

    threads = [threading.Thread(target=insert) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(created) == [False, False, False, True]
    assert count(connect("postgres"), "payments.payments") == 1


# The money check's SQL on PostgreSQL --------------------------------------------------


class Records:
    """The provider's side of the check: its events, and the balance they add up to."""

    def __init__(self, *events: Event) -> None:
        self.events = list(events)

    def balance_cents(self) -> int:
        return sum(MONEY_EFFECT[e.type] * e.amount_cents for e in self.events)


PAID = Event("evt_000001", PAYMENT_SUCCEEDED, "pay_000001", 2_500, 0)
REFUNDED = Event("evt_000002", REFUND_SUCCEEDED, "pay_000001", 1_000, 0)


def readonly_snapshot(connect):
    conn = connect("klaxon_readonly")
    conn.autocommit = False
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    return conn


def test_the_money_check_passes_on_a_correct_postgres_ledger(pool_as, connect):
    ledger = PgLedger(pool_as("ledger_writer"))
    ledger.book(PAYMENT, "a")
    ledger.book(REFUND, "b")
    report = check_money(Records(PAID, REFUNDED), readonly_snapshot(connect))
    assert report.passed, report
    assert report.ledger_clearing_cents == report.provider_balance_cents == 1_500


def test_the_money_check_catches_a_double_posting_on_postgres_with_the_same_evidence(pool_as, connect):
    ledger = PgLedger(pool_as("ledger_writer"), "naive")
    ledger.book(PAYMENT, "provider-events/0@0")
    ledger.book(PAYMENT, "provider-events/0@1")
    provider = Records(PAID)
    report = check_money(provider, readonly_snapshot(connect))
    assert not report.passed
    assert report.invariant("ledger_balanced").passed  # the trap: a double posting is itself balanced
    [problem] = report.invariant("posted_exactly_once").evidence
    assert (problem["event_id"], problem["times_posted"], problem["transaction_ids"]) == ("evt_000001", 2, [1, 2])
    assert report.money_off_cents == 2_500
    assert isinstance(report.money_off_cents, int)  # BIGINT casts: no Decimal sneaks in


def test_a_reversal_repairs_the_double_posting_and_the_check_passes_again(pool_as, connect):
    ledger = PgLedger(pool_as("ledger_writer"), "naive")
    ledger.book(PAYMENT, "provider-events/0@0")
    ledger.book(PAYMENT, "provider-events/0@1")
    writer = connect("ledger_writer")
    with writer.transaction():
        (reversal,) = writer.execute(
            "INSERT INTO transactions (event_id, payment_id, description, reverses_transaction_id)"
            " VALUES ('evt_000001', 'pay_000001', 'reversal of #2: duplicate delivery', 2) RETURNING id"
        ).fetchone()
        writer.execute(
            "INSERT INTO entries (transaction_id, account, side, amount_cents) VALUES"
            " (%s, 'provider_clearing', 'credit', 2500), (%s, 'sales', 'debit', 2500)",
            (reversal, reversal),
        )
    provider = Records(PAID)
    assert check_money(provider, readonly_snapshot(connect)).passed


def test_the_money_check_catches_an_unbalanced_transaction_on_postgres(connect):
    admin = connect("postgres")  # only a superuser can write a broken posting by hand
    admin.execute(
        "INSERT INTO ledger.transactions (event_id, payment_id, description) VALUES ('evt_000001', 'pay_000001', 'x')"
    )
    admin.execute(
        "INSERT INTO ledger.entries (transaction_id, account, side, amount_cents) VALUES (1, 'sales', 'credit', 2500)"
    )
    provider = Records(PAID)
    report = check_money(provider, readonly_snapshot(connect))
    balanced = report.invariant("ledger_balanced")
    assert not balanced.passed
    assert {"transaction_id": 1, "lines": 1, "debits_cents": 0, "credits_cents": 2_500} in balanced.evidence
