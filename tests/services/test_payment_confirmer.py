"""The payment confirmer's decisions, offline. Its SQL runs against real PostgreSQL in tests/integration."""

import pytest
from psycopg_pool import PoolTimeout

from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED
from klaxon.patient.services.events import ProviderEvent
from klaxon.patient.services.payment_confirmer import (
    OurPayment,
    OurRefund,
    PgConfirmer,
    require_grants,
    why_refund_unchanged,
    why_unchanged,
)

EVENT = ProviderEvent("evt_000003", PAYMENT_SUCCEEDED, 1, "pay_000003", 6_968, "usd", "pmt_000003")


def ours(status="succeeded", amount=6_968, provider_id="pay_000003") -> OurPayment:
    return OurPayment("pmt_000003", status, amount, provider_id)


@pytest.mark.parametrize(
    ("payment", "outcome", "problem"),
    [
        (ours(), "already_confirmed", None),  # the API's own answer got there first: the usual case
        (ours(status="failed", provider_id=None), "conflict", "the provider charged a payment we recorded as failed"),
        (ours(amount=69_68_00), "conflict", "the provider charged a different amount than our payment's"),
        (ours(status="pending", amount=1, provider_id=None), "conflict", "the provider charged a different amount"),
        (ours(provider_id="pay_000009"), "conflict", "our payment is linked to another provider payment (pay_000009)"),
        (None, "unknown_payment", "the provider charged pmt_000003, which is not in our records"),
    ],
)
def test_why_a_confirmation_changed_nothing(payment, outcome, problem):
    got_outcome, got_problem = why_unchanged(EVENT, payment, "by_our_id")
    assert got_outcome == outcome
    assert got_problem is None if problem is None else got_problem.startswith(problem)


def test_an_event_without_our_id_names_no_payment_of_ours():
    event = ProviderEvent("evt_1", PAYMENT_SUCCEEDED, 1, "pay_000001", 100, "usd", None)
    assert why_unchanged(event, None, "by_our_id") == (
        "unknown_payment",
        "the provider charged a payment whose metadata names no payment of ours",
    )


def test_the_naive_confirmer_reads_not_found_as_nothing_to_do():
    """The known-bad baseline: no error, no warning. That silence is why only the money check notices."""
    assert why_unchanged(EVENT, None, "by_provider_id") == ("not_found", None)


class NoDatabase:
    def connection(self):
        raise AssertionError("this event should not touch the database")


def test_events_that_are_neither_a_charge_nor_a_refund_are_not_the_confirmers_business():
    other = ProviderEvent("evt_2", "payment.updated", 1, "pay_000001", 100, "usd", None)
    assert PgConfirmer(NoDatabase()).confirm(other, "provider-events/0@1") == "ignored_type"


# Refunds (week 2, item 6) ---------------------------------------------------------------

REFUND = ProviderEvent("evt_000007", REFUND_SUCCEEDED, 1, "pay_000003", 3_484, "usd", None, "rfd_000001", "re_000001")


def our_refund(status="succeeded", amount=3_484, provider_refund="re_000001", payment="pay_000003") -> OurRefund:
    return OurRefund("rfd_000001", status, amount, provider_refund, payment)


@pytest.mark.parametrize(
    ("refund", "outcome", "problem"),
    [
        (our_refund(), "refund_already_confirmed", None),  # the API's own answer got there first
        (
            our_refund(status="failed", provider_refund=None),
            "refund_conflict",
            "the provider refunded a refund we recorded as failed",
        ),
        (our_refund(amount=1), "refund_conflict", "the provider refunded a different amount than our refund's"),
        (
            our_refund(provider_refund="re_000009"),
            "refund_conflict",
            "our refund is linked to another provider refund (re_000009)",
        ),
        (
            our_refund(payment="pay_000009"),
            "refund_conflict",
            "the provider refunded another payment than the one our refund is for",
        ),
        (None, "unknown_refund", "the provider refunded rfd_000001, which is not in our records"),
    ],
)
def test_why_a_refunds_confirmation_changed_nothing(refund, outcome, problem):
    got_outcome, got_problem = why_refund_unchanged(REFUND, refund, "by_our_id")
    assert got_outcome == outcome
    assert got_problem is None if problem is None else got_problem == problem


def test_the_naive_confirmer_reads_a_refund_not_found_as_nothing_to_do():
    assert why_refund_unchanged(REFUND, None, "by_provider_id") == ("refund_not_found", None)


def test_a_refund_event_without_our_id_is_reported_without_a_query(caplog):
    event = ProviderEvent("evt_1", REFUND_SUCCEEDED, 1, "pay_000001", 100, "usd", None, None, "re_000001")
    with caplog.at_level("ERROR"):
        assert PgConfirmer(NoDatabase()).confirm(event, "provider-events/0@1") == "unknown_refund"
    [line] = [r for r in caplog.records if r.name == "klaxon.payment_confirmer"]
    assert line.getMessage() == "refund not confirmed: the provider refunded with metadata that names no refund of ours"
    assert (line.fields["provider_refund_id"], line.fields["refund_id"]) == ("re_000001", None)


def test_a_refund_event_without_the_providers_refund_id_is_reported_without_a_query(caplog):
    """Our refund could only be confirmed unlinked, or one already linked be called a conflict: neither is true."""
    event = ProviderEvent("evt_1", REFUND_SUCCEEDED, 1, "pay_000001", 100, "usd", None, "rfd_000001", None)
    with caplog.at_level("ERROR"):
        assert PgConfirmer(NoDatabase()).confirm(event, "provider-events/0@1") == "unknown_refund"
    [line] = [r for r in caplog.records if r.name == "klaxon.payment_confirmer"]
    assert line.getMessage() == "refund not confirmed: the provider's refund event names no plain refund id of its own"
    assert line.fields["refund_id"] == "rfd_000001"


def test_the_startup_check_lets_the_confirmer_start_while_the_database_is_down(caplog):
    """It must wait for the database like the loop does, not crash-loop: an outage at startup is still an outage."""

    class DownPool:
        def connection(self):
            raise PoolTimeout("couldn't get a connection after 5.00 sec")

    with caplog.at_level("WARNING"):
        require_grants(DownPool())  # no OutdatedDatabase, no crash
    [line] = [r for r in caplog.records if r.name == "klaxon.payment_confirmer"]
    assert line.getMessage() == "could not check the database's grants: it is not reachable"


def test_the_naive_confirmer_without_the_providers_refund_id_does_not_query_either():
    event = ProviderEvent("evt_1", REFUND_SUCCEEDED, 1, "pay_000001", 100, "usd", None, "rfd_000001", None)
    assert PgConfirmer(NoDatabase(), "by_provider_id").confirm(event, "provider-events/0@1") == "refund_not_found"


def test_a_payment_event_without_our_id_is_reported_without_a_query(caplog):
    event = ProviderEvent("evt_1", PAYMENT_SUCCEEDED, 1, "pay_000001", 100, "usd", None)
    with caplog.at_level("ERROR"):
        assert PgConfirmer(NoDatabase()).confirm(event, "provider-events/0@1") == "unknown_payment"
    [line] = [r for r in caplog.records if r.name == "klaxon.payment_confirmer"]
    assert line.getMessage().startswith("not confirmed: the provider charged a payment whose metadata")
    assert line.fields["provider_payment_id"] == "pay_000001"
    assert line.fields["payment_id"] is None and "klaxon_payment_id" not in line.fields  # payment_id is always ours


def test_only_the_two_modes_exist():
    with pytest.raises(ValueError):
        PgConfirmer(NoDatabase(), "by_customer")
