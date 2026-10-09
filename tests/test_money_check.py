"""The first piece's acceptance tests: the dashboard stays green, the money check does not."""

import pytest

from klaxon.checks.money import MONEY_EFFECT, check_money, format_cents
from klaxon.patient.dashboard import summarize
from klaxon.patient.ledger import Line, posting_lines
from klaxon.patient.provider import Event, PlannedDelivery
from tests.conftest import make_endpoint


def deliver(provider, mode, plan):
    ledger, endpoint = make_endpoint(provider, mode)
    provider.deliver(endpoint, plan)
    return ledger, endpoint


def test_naive_handler_and_a_duplicate_fail_the_money_check_with_exact_evidence(provider):
    ledger, endpoint = deliver(provider, "naive", provider.plan("duplicate", duplicate_event_id="evt_0002"))
    report = check_money(provider, ledger.conn)

    assert not report.passed
    once = report.invariant("posted_exactly_once")
    assert not once.passed
    assert once.evidence == [
        {
            "event_id": "evt_0002",
            "type": "payment.succeeded",
            "payment_id": "pay_0002",
            "amount_cents": 4_900,
            "times_posted": 2,
            "expected": 1,
            "problem": "counted twice",
            "transaction_ids": [2, 5],  # which postings to look at, for whoever repairs it
        }
    ]
    matches = report.invariant("matches_provider")
    assert not matches.passed
    assert {"scope": "pay_0002", "ledger_cents": 9_800, "provider_cents": 4_900, "difference_cents": 4_900} in (
        matches.evidence
    )
    assert report.money_off_cents == 4_900  # the books claim $49.00 the provider never had

    # The trap: the double posting is itself balanced, so this invariant alone would miss it.
    assert report.invariant("ledger_balanced").passed
    # And HTTP monitoring sees nothing wrong.
    assert summarize(endpoint.requests).status == "GREEN"


def test_idempotent_handler_and_the_same_duplicate_pass(provider):
    ledger, endpoint = deliver(provider, "idempotent", provider.plan("duplicate", duplicate_event_id="evt_0002"))
    report = check_money(provider, ledger.conn)
    assert report.passed, report
    assert report.money_off_cents == 0
    assert summarize(endpoint.requests).status == "GREEN"


def test_reversing_the_duplicate_posting_repairs_the_books_and_the_check_passes(provider):
    # How a fix will repair the damage: never delete a ledger row, post its reversal.
    ledger, _ = deliver(provider, "naive", provider.plan("duplicate", duplicate_event_id="evt_0002"))
    duplicate = check_money(provider, ledger.conn).invariant("posted_exactly_once").evidence[0]["transaction_ids"][-1]
    ledger.reverse(duplicate, reason="duplicate delivery of evt_0002", posted_at=provider.clock.now())
    report = check_money(provider, ledger.conn)
    assert report.passed, report
    assert report.money_off_cents == 0


def test_reversing_the_only_posting_makes_the_event_lost(provider):
    ledger, _ = deliver(provider, "idempotent", provider.plan("normal"))
    ledger.reverse(3, reason="reversed by mistake", posted_at=0)  # evt_0003's only posting
    once = check_money(provider, ledger.conn).invariant("posted_exactly_once")
    assert once.evidence[0]["event_id"] == "evt_0003"
    assert once.evidence[0]["problem"] == "lost"


@pytest.mark.parametrize("mode", ["naive", "idempotent"])
def test_dashboard_is_green_whether_or_not_the_money_is_right(provider, mode):
    _, endpoint = deliver(provider, mode, provider.plan("duplicate", duplicate_event_id="evt_0002"))
    dashboard = summarize(endpoint.requests)
    assert dashboard.requests == 5
    assert dashboard.ok == 5
    assert dashboard.error_rate == 0.0
    assert dashboard.status == "GREEN"


@pytest.mark.parametrize("mode", ["naive", "idempotent"])
def test_normal_delivery_passes_with_either_handler(provider, mode):
    # The naive handler's bug is latent: it only shows when a duplicate arrives.
    ledger, _ = deliver(provider, mode, provider.plan("normal"))
    assert check_money(provider, ledger.conn).passed


@pytest.mark.parametrize("mode", ["naive", "idempotent"])
def test_out_of_order_delivery_still_balances_and_matches(provider, mode):
    ledger, _ = deliver(provider, mode, provider.plan("out_of_order"))
    report = check_money(provider, ledger.conn)
    assert report.passed, report


def test_a_refund_that_arrives_before_its_payment_still_matches(provider):
    refund, payment = provider.event("evt_0004"), provider.event("evt_0001")
    plan = [PlannedDelivery(refund), PlannedDelivery(payment)] + [
        PlannedDelivery(e) for e in provider.events if e.id not in ("evt_0001", "evt_0004")
    ]
    ledger, _ = deliver(provider, "idempotent", plan)
    assert check_money(provider, ledger.conn).passed


def test_a_lost_event_is_caught_too(provider):
    plan = [PlannedDelivery(e) for e in provider.events if e.id != "evt_0003"]
    ledger, _ = deliver(provider, "idempotent", plan)
    report = check_money(provider, ledger.conn)
    once = report.invariant("posted_exactly_once")
    assert once.evidence[0]["event_id"] == "evt_0003"
    assert once.evidence[0]["times_posted"] == 0
    assert once.evidence[0]["problem"] == "lost"
    assert report.money_off_cents == -1_250  # the books are missing $12.50


def test_a_transaction_with_no_lines_does_not_count_as_a_posting(provider):
    # A posting that lost its lines must not hide a lost payment.
    plan = [PlannedDelivery(e) for e in provider.events if e.id != "evt_0003"]
    ledger, _ = deliver(provider, "idempotent", plan)
    ledger.conn.execute(
        "INSERT INTO transactions (event_id, payment_id, description, posted_at) VALUES ('evt_0003', 'pay_0003', 'empty', 0)"
    )
    report = check_money(provider, ledger.conn)
    assert report.invariant("posted_exactly_once").evidence[0]["problem"] == "lost"
    balanced = report.invariant("ledger_balanced")
    assert not balanced.passed
    assert {"transaction_id": 4, "lines": 0, "debits_cents": 0, "credits_cents": 0} in balanced.evidence


def test_a_posting_the_provider_never_sent_is_caught(provider):
    ledger, _ = deliver(provider, "idempotent", provider.plan("normal"))
    ledger.post(
        event_id="evt_ghost",
        payment_id="pay_0003",
        description="manual adjustment",
        lines=[Line("provider_clearing", "debit", 700), Line("sales", "credit", 700)],
        posted_at=0,
    )
    report = check_money(provider, ledger.conn)
    once = report.invariant("posted_exactly_once")
    assert once.evidence == [
        {
            "event_id": "evt_ghost",
            "times_posted": 1,
            "expected": 0,
            "problem": "posted, but the provider never sent it",
            "transaction_ids": [5],
        }
    ]
    assert report.money_off_cents == 700


def test_an_unbalanced_transaction_written_behind_the_ledgers_back_is_caught(provider):
    ledger, _ = deliver(provider, "idempotent", provider.plan("normal"))
    # Bypass Ledger.post (which refuses this) the way a buggy migration might.
    ledger.conn.execute("INSERT INTO entries (transaction_id, account, side, amount_cents) VALUES (1, 'sales', 'credit', 5)")
    balanced = check_money(provider, ledger.conn).invariant("ledger_balanced")
    assert not balanced.passed
    assert {"transaction_id": 1, "lines": 3, "debits_cents": 2_500, "credits_cents": 2_505} in balanced.evidence


def test_money_moved_between_payments_is_caught_even_when_totals_agree(provider):
    ledger, _ = deliver(provider, "idempotent", provider.plan("normal"))
    # Move one posting of $12.50 from pay_0003 to pay_0002: the overall totals do not change.
    ledger.conn.execute("UPDATE transactions SET payment_id = 'pay_0002' WHERE event_id = 'evt_0003'")
    matches = check_money(provider, ledger.conn).invariant("matches_provider")
    assert not matches.passed
    assert matches.summary.startswith("totals agree")
    assert {e["scope"] for e in matches.evidence} == {"pay_0002", "pay_0003"}


def test_events_that_move_no_money_are_ignored_not_a_crash(provider):
    provider.events.append(Event("evt_9999", "customer.updated", "pay_0001", 1, provider.clock.now()))
    ledger, _ = deliver(provider, "idempotent", provider.plan("normal"))
    assert check_money(provider, ledger.conn).passed


def test_the_check_and_the_ledger_agree_on_which_events_move_money():
    booked = {t for t in ("payment.succeeded", "refund.succeeded", "customer.updated") if posting_lines(t, 1)}
    assert booked == set(MONEY_EFFECT)


@pytest.mark.parametrize(
    ("cents", "signed", "text"),
    [(0, False, "$0.00"), (5, False, "$0.05"), (4_900, True, "+$49.00"), (-1_250, False, "-$12.50"), (123_456_78, False, "$123,456.78")],
)
def test_format_cents(cents, signed, text):
    assert format_cents(cents, signed=signed) == text
