"""The payment confirmer and invariants 4 and 5 in real PostgreSQL: what changes, what never does, who may do it."""

import threading

import psycopg
import pytest

from klaxon.checks.live import read_ledger_and_check
from klaxon.checks.money import MONEY_EFFECT, ProviderPayment, ProviderRefund, check_money
from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED, Event
from klaxon.patient.services.events import ProviderEvent
from klaxon.patient.services.payment_confirmer import OutdatedDatabase, PgConfirmer, require_grants
from klaxon.patient.services.payments_store import PgPaymentsStore
from klaxon.patient.services.pg_ledger import PgLedger
from tests.integration.conftest import dsn

pytestmark = pytest.mark.integration


def charged(ours: str | None = "pmt_000001", provider: str = "pay_000001", amount: int = 2_500, n: int = 1):
    """The provider's payment.succeeded event, with our id in its metadata."""
    return ProviderEvent(f"evt_{n:06d}", PAYMENT_SUCCEEDED, 1_791_504_000 + n, provider, amount, "usd", ours)


@pytest.fixture
def api_store(pool_as) -> PgPaymentsStore:
    """Our payments table, written the way the payments API writes it."""
    return PgPaymentsStore(pool_as("payments_api", dict_rows=True))


@pytest.fixture
def confirmer(pool_as) -> PgConfirmer:
    return PgConfirmer(pool_as("payment_confirmer"))


def pending(api_store, key="key-1", amount=2_500):
    """A payment the API sent to the provider, whose answer never came back."""
    row, created = api_store.insert_or_get_payment(key, "fp", "cus_001", amount, "usd")
    assert created and row.status == "pending" and row.provider_payment_id is None
    return row


def test_a_payment_whose_answer_was_lost_is_confirmed_by_its_webhook(api_store, confirmer):
    row = pending(api_store)
    assert confirmer.confirm(charged(), "provider-events/0@0") == "confirmed"
    after = api_store.get_payment(row.id)
    assert (after.status, after.provider_payment_id, after.amount_cents) == ("succeeded", "pay_000001", 2_500)


def test_a_repeated_event_changes_nothing(api_store, confirmer):
    pending(api_store)
    assert confirmer.confirm(charged(), "provider-events/0@0") == "confirmed"
    assert confirmer.confirm(charged(), "provider-events/0@1") == "already_confirmed"


def test_when_the_apis_own_answer_got_there_first_the_webhook_is_a_no_op(api_store, confirmer):
    row = pending(api_store)
    api_store.mark_payment(row.id, "succeeded", "pay_000001")
    assert confirmer.confirm(charged(), "provider-events/0@0") == "already_confirmed"


def test_a_failed_payment_is_never_overwritten(api_store, confirmer):
    row = pending(api_store)
    api_store.mark_payment(row.id, "failed", None)
    assert confirmer.confirm(charged(), "provider-events/0@0") == "conflict"
    assert api_store.get_payment(row.id).status == "failed"  # for a human to look at, not for us to guess


def test_a_charge_for_a_different_amount_is_never_confirmed(api_store, confirmer):
    row = pending(api_store)
    assert confirmer.confirm(charged(amount=250_000), "provider-events/0@0") == "conflict"
    after = api_store.get_payment(row.id)
    assert (after.status, after.provider_payment_id) == ("pending", None)


def test_a_second_charge_for_the_same_payment_is_a_conflict_not_a_new_link(api_store, confirmer):
    row = pending(api_store)
    assert confirmer.confirm(charged(provider="pay_000001", n=1), "provider-events/0@0") == "confirmed"
    assert confirmer.confirm(charged(provider="pay_000002", n=2), "provider-events/0@1") == "conflict"
    assert api_store.get_payment(row.id).provider_payment_id == "pay_000001"


def test_a_pending_payment_already_linked_to_another_charge_is_not_relinked(api_store, confirmer):
    # Not a state the API makes today, but one a "store the provider's id first" flow would: never swap the link.
    row = pending(api_store)
    api_store.mark_payment(row.id, "pending", "pay_000009")
    assert confirmer.confirm(charged(provider="pay_000001"), "provider-events/0@0") == "conflict"
    after = api_store.get_payment(row.id)
    assert (after.status, after.provider_payment_id) == ("pending", "pay_000009")


def test_two_of_our_payments_claiming_one_charge_is_a_conflict_not_a_dead_letter(api_store, confirmer):
    first = pending(api_store)
    api_store.mark_payment(first.id, "succeeded", "pay_000001")
    second = pending(api_store, key="key-2")
    # The provider says pay_000001 is for the second payment, but the first one already holds that link.
    assert confirmer.confirm(charged(ours=second.id, provider="pay_000001"), "provider-events/0@0") == "conflict"
    assert (api_store.get_payment(second.id).status, api_store.get_payment(first.id).provider_payment_id) == (
        "pending",
        "pay_000001",
    )


def test_an_id_we_never_issued_is_reported(confirmer):
    assert confirmer.confirm(charged(ours="pmt_000404"), "provider-events/0@0") == "unknown_payment"


def test_the_naive_confirmer_leaves_a_payment_whose_answer_was_lost_pending(pool_as, api_store):
    naive = PgConfirmer(pool_as("payment_confirmer"), "by_provider_id")
    stuck = pending(api_store)
    assert naive.confirm(charged(), "provider-events/0@0") == "not_found"
    assert api_store.get_payment(stuck.id).status == "pending"
    # Once the API has heard the provider's answer, the naive version looks fine. That is why it passes normal tests.
    heard = pending(api_store, key="key-2")
    api_store.mark_payment(heard.id, "succeeded", "pay_000002")
    assert naive.confirm(charged(ours=heard.id, provider="pay_000002", n=2), "provider-events/0@1") == (
        "already_confirmed"
    )


def test_the_api_and_the_confirmer_racing_on_one_payment_agree(api_store, confirmer):
    """Both write the same values, so this shows no error, no deadlock and one consistent end state.
    That a final status is never overwritten is tested above."""
    row = pending(api_store)
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def api_answer():
        start.wait()
        api_store.mark_payment(row.id, "succeeded", "pay_000001")

    def webhook():
        start.wait()
        outcomes.append(confirmer.confirm(charged(), "provider-events/0@0"))

    threads = [threading.Thread(target=api_answer), threading.Thread(target=webhook)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes[0] in ("confirmed", "already_confirmed")
    after = api_store.get_payment(row.id)
    assert (after.status, after.provider_payment_id) == ("succeeded", "pay_000001")


# Refunds (week 2, item 6) ---------------------------------------------------------------


def refunded(ours="rfd_000001", provider_refund="re_000001", payment="pay_000001", amount=1_000, n=9):
    """The provider's refund.succeeded event, with our refund id in its metadata and its own refund id."""
    return ProviderEvent(
        f"evt_{n:06d}", REFUND_SUCCEEDED, 1_791_504_000 + n, payment, amount, "usd", None, ours, provider_refund
    )


def pending_refund(api_store, key="refund-1", amount=1_000, payment_key="key-1", provider_payment="pay_000001"):
    """A refund of a succeeded payment, sent to the provider, whose answer never came back."""
    row, _ = api_store.insert_or_get_payment(payment_key, "fp", "cus_001", 2_500, "usd")
    if row.status != "succeeded":
        api_store.mark_payment(row.id, "succeeded", provider_payment)
    refund, created = api_store.insert_or_get_refund(key, "fp", row.id, amount)
    assert created and refund.status == "pending" and refund.provider_refund_id is None
    return refund


def our_refund(api_store, refund):
    [row] = [r for r in api_store.refunds_for(refund.payment_id) if r.id == refund.id]
    return row


def test_a_refund_whose_answer_was_lost_is_confirmed_by_its_webhook(api_store, confirmer):
    refund = pending_refund(api_store)
    assert confirmer.confirm(refunded(ours=refund.id), "provider-events/0@0") == "refund_confirmed"
    after = our_refund(api_store, refund)
    assert (after.status, after.provider_refund_id, after.amount_cents) == ("succeeded", "re_000001", 1_000)
    assert confirmer.confirm(refunded(ours=refund.id), "provider-events/0@1") == "refund_already_confirmed"


def test_when_the_apis_own_answer_got_there_first_the_refunds_webhook_is_a_no_op(api_store, confirmer):
    refund = pending_refund(api_store)
    api_store.mark_refund(refund.id, "succeeded", "re_000001")
    assert confirmer.confirm(refunded(ours=refund.id), "provider-events/0@0") == "refund_already_confirmed"


@pytest.mark.parametrize(
    ("event_changes", "setup", "problem"),
    [
        ({"amount": 999}, None, "different amount"),
        ({"payment": "pay_000002"}, None, "another payment"),
        ({}, "failed", "recorded as failed"),
        ({}, "linked", "linked to another provider refund"),
    ],
)
def test_a_refund_the_provider_reports_otherwise_is_never_confirmed(
    api_store, confirmer, caplog, event_changes, setup, problem
):
    refund = pending_refund(api_store)
    if setup == "failed":
        api_store.mark_refund(refund.id, "failed", None)
    if setup == "linked":
        api_store.mark_refund(refund.id, "pending", "re_000009")
    with caplog.at_level("ERROR"):
        assert confirmer.confirm(refunded(ours=refund.id, **event_changes), "provider-events/0@0") == "refund_conflict"
    assert problem in caplog.text
    after = our_refund(api_store, refund)
    assert after.status == ("failed" if setup == "failed" else "pending") and after.amount_cents == 1_000


def test_two_of_our_refunds_claiming_one_provider_refund_is_a_conflict_not_a_dead_letter(api_store, confirmer):
    first = pending_refund(api_store)
    second = pending_refund(api_store, key="refund-2")
    api_store.mark_refund(first.id, "succeeded", "re_000001")
    assert confirmer.confirm(refunded(ours=second.id), "provider-events/0@0") == "refund_conflict"
    assert our_refund(api_store, second).status == "pending"


@pytest.mark.parametrize("linked", [None, "re_000001"])
def test_a_refund_event_naming_no_provider_refund_changes_nothing(api_store, confirmer, linked):
    """Neither confirmed unlinked, nor, when the API already linked it, reported as linked to another refund."""
    refund = pending_refund(api_store)
    if linked:
        api_store.mark_refund(refund.id, "succeeded", linked)
    assert confirmer.confirm(refunded(ours=refund.id, provider_refund=None), "provider-events/0@0") == "unknown_refund"
    after = our_refund(api_store, refund)
    assert (after.status, after.provider_refund_id) == (("succeeded", linked) if linked else ("pending", None))


def test_a_refund_id_we_never_issued_is_reported(confirmer):
    assert confirmer.confirm(refunded(ours="rfd_999999"), "provider-events/0@0") == "unknown_refund"


def test_the_naive_confirmer_leaves_a_refund_whose_answer_was_lost_pending(pool_as, api_store):
    naive = PgConfirmer(pool_as("payment_confirmer"), "by_provider_id")
    stuck = pending_refund(api_store)
    assert naive.confirm(refunded(ours=stuck.id), "provider-events/0@0") == "refund_not_found"
    assert our_refund(api_store, stuck).status == "pending"


def test_the_api_and_the_confirmer_racing_on_one_refund_agree(api_store, confirmer):
    refund = pending_refund(api_store)
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def api_answer():
        start.wait()
        api_store.mark_refund(refund.id, "succeeded", "re_000001")

    def webhook():
        start.wait()
        outcomes.append(confirmer.confirm(refunded(ours=refund.id), "provider-events/0@0"))

    threads = [threading.Thread(target=api_answer), threading.Thread(target=webhook)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes[0] in ("refund_confirmed", "refund_already_confirmed")
    after = our_refund(api_store, refund)
    assert (after.status, after.provider_refund_id) == ("succeeded", "re_000001")


# Permissions --------------------------------------------------------------------------


def test_the_confirmer_refuses_to_start_on_a_database_made_before_the_refund_grants(pool_as, connect):
    """Its setup scripts run only on a new volume. Without the grants, every refund event would be retried forever."""
    require_grants(pool_as("payment_confirmer"))  # a database from today's schema: fine
    connect("postgres").execute("REVOKE UPDATE (provider_refund_id) ON payments.refunds FROM payment_confirmer")
    with pytest.raises(OutdatedDatabase, match="make reset"):
        require_grants(pool_as("payment_confirmer"))


@pytest.mark.parametrize(
    "statement",
    [
        # An explicit id: without one, the id sequence would refuse it first, and hide whether INSERT is granted.
        "INSERT INTO payments (id, idempotency_key, request_fingerprint, customer, amount_cents, currency, status)"
        " VALUES ('pmt_forged', 'k', 'f', 'cus_x', 1, 'usd', 'succeeded')",
        "UPDATE payments SET amount_cents = 1",
        "UPDATE payments SET customer = 'someone else'",
        "DELETE FROM payments",
        "SELECT customer FROM payments",  # it reads only the four columns it compares
        "SELECT idempotency_key FROM payments",
        "SELECT * FROM payments.refunds",  # its refunds: the five columns it compares, no more
        "SELECT idempotency_key FROM payments.refunds",
        "UPDATE payments.refunds SET amount_cents = 1",
        "UPDATE payments.refunds SET payment_id = 'pmt_other'",
        "INSERT INTO payments.refunds (id, payment_id, idempotency_key, request_fingerprint, amount_cents, status)"
        " VALUES ('rfd_forged', 'pmt_000001', 'k', 'f', 1, 'succeeded')",
        "DELETE FROM payments.refunds",
        "SELECT * FROM ledger.entries",
        "INSERT INTO ledger.processed_events (event_id) VALUES ('evt_forged')",
    ],
)
def test_the_confirmer_can_move_a_status_and_nothing_else(api_store, connect, statement):
    pending(api_store)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        connect("payment_confirmer").execute(statement)


# The fourth and fifth invariants' SQL on PostgreSQL -----------------------------------


class Records:
    """The provider's side: its charges and refunds as events and as records, and the balance they add up to."""

    def __init__(self, *charges: tuple[str, int, str | None], refunds: tuple = ()) -> None:
        self.payments = [ProviderPayment(pid, cents, ours) for pid, cents, ours in charges]
        self.refunds = [ProviderRefund(rid, pid, cents, ours) for rid, pid, cents, ours in refunds]
        self.events = [
            Event(f"evt_{n:06d}", PAYMENT_SUCCEEDED, p.id, p.amount_cents, n) for n, p in enumerate(self.payments, 1)
        ] + [
            Event(f"evt_{n:06d}", REFUND_SUCCEEDED, r.payment_id, r.amount_cents, n)
            for n, r in enumerate(self.refunds, len(self.payments) + 1)
        ]

    def balance_cents(self) -> int:
        return sum(MONEY_EFFECT[e.type] * e.amount_cents for e in self.events)


def readonly_snapshot(connect):
    conn = connect("klaxon_readonly")
    conn.autocommit = False
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    return conn


def test_the_fourth_invariant_sees_a_stuck_payment_until_its_webhook_confirms_it(
    pool_as, api_store, confirmer, connect
):
    row = pending(api_store)
    event = charged(ours=row.id)
    PgLedger(pool_as("ledger_writer")).book(event, "provider-events/0@0")  # the ledger booked the charge
    provider = Records(("pay_000001", 2_500, row.id))

    report = check_money(provider, readonly_snapshot(connect), provider.payments)
    assert [i.passed for i in report.invariants] == [True, True, True, False]  # the books are right; our record is not
    assert report.invariant("payments_match_provider").evidence[0]["problem"] == "pending but charged"
    assert isinstance(report.invariant("payments_match_provider").evidence[0]["amount_cents"], int)

    assert confirmer.confirm(event, "provider-events/0@0") == "confirmed"
    assert check_money(provider, readonly_snapshot(connect), provider.payments).passed


def test_the_fifth_invariant_sees_a_stuck_refund_until_its_webhook_confirms_it(pool_as, api_store, confirmer, connect):
    refund = pending_refund(api_store)
    event = refunded(ours=refund.id, n=2)  # the provider's second event, after the charge
    ledger = PgLedger(pool_as("ledger_writer"))
    ledger.book(charged(ours=refund.payment_id), "provider-events/0@0")
    ledger.book(event, "provider-events/0@1")  # the ledger booked the refund
    provider = Records(
        ("pay_000001", 2_500, refund.payment_id), refunds=[("re_000001", "pay_000001", 1_000, refund.id)]
    )

    report = check_money(provider, readonly_snapshot(connect), provider.payments, provider_refunds=provider.refunds)
    assert [i.passed for i in report.invariants] == [True, True, True, True, False]  # only our refund is wrong
    [stuck] = report.invariant("refunds_match_provider").evidence
    assert (stuck["problem"], stuck["provider_payment_id"]) == ("pending but refunded", "pay_000001")
    assert isinstance(stuck["amount_cents"], int)

    assert confirmer.confirm(event, "provider-events/0@1") == "refund_confirmed"
    after = check_money(provider, readonly_snapshot(connect), provider.payments, provider_refunds=provider.refunds)
    assert after.passed, after


def test_the_live_check_runs_all_five_invariants_through_the_read_only_role(api_store, database):
    refund = pending_refund(api_store)
    api_store.mark_refund(refund.id, "succeeded", "re_000001")
    provider = Records(
        ("pay_000001", 2_500, refund.payment_id),
        ("pay_000002", 2_500, refund.payment_id),  # charged twice
        refunds=[("re_000001", "pay_000001", 1_000, refund.id), ("re_000002", "pay_000001", 1_000, refund.id)],
    )
    _, report = read_ledger_and_check(None, dsn("klaxon_readonly", database), lambda as_of: provider)
    assert [i.name for i in report.invariants][-2:] == ["payments_match_provider", "refunds_match_provider"]
    assert report.invariant("payments_match_provider").evidence[0]["problem"] == "charged twice"
    assert report.invariant("refunds_match_provider").evidence[0]["problem"] == "refunded twice"
