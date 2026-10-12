"""The payment confirmer's decisions, offline. Its SQL runs against real PostgreSQL in tests/integration."""

import pytest

from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED
from klaxon.patient.services.events import ProviderEvent
from klaxon.patient.services.payment_confirmer import OurPayment, PgConfirmer, why_unchanged

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


def test_refund_events_are_not_the_confirmers_business():
    refund = ProviderEvent("evt_2", REFUND_SUCCEEDED, 1, "pay_000001", 100, "usd", None)
    assert PgConfirmer(NoDatabase()).confirm(refund, "provider-events/0@1") == "ignored_type"


def test_a_payment_event_without_our_id_is_reported_without_a_query(caplog):
    event = ProviderEvent("evt_1", PAYMENT_SUCCEEDED, 1, "pay_000001", 100, "usd", None)
    with caplog.at_level("ERROR"):
        assert PgConfirmer(NoDatabase()).confirm(event, "provider-events/0@1") == "unknown_payment"
    [line] = [r for r in caplog.records if r.name == "klaxon.payment_confirmer"]
    assert line.getMessage().startswith("not confirmed: the provider charged a payment whose metadata")
    assert line.fields["provider_payment_id"] == "pay_000001"


def test_only_the_two_modes_exist():
    with pytest.raises(ValueError):
        PgConfirmer(NoDatabase(), "by_customer")
