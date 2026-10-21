"""The money check's fifth invariant: our refunds table against the provider's refunds.

Offline, on SQLite, like test_payments_match_provider: the first piece's ledger, which books every event the
provider sent correctly, plus payments and refunds tables with the live schema's columns. The same SQL runs on
PostgreSQL in tests/integration.
"""

import pytest

from klaxon.checks.money import MONEY_EFFECT, OVER_REFUNDED, ProviderPayment, ProviderRefund, check_money
from klaxon.patient.ledger import Ledger, posting_lines
from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED, Event

FOURTH, FIFTH = "payments_match_provider", "refunds_match_provider"
CUTOFF = 1_000


class Provider:
    """The provider's side: its charges and refunds, one event each, and its balance as of the cutoff."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.payments: list[ProviderPayment] = []
        self.refunds: list[ProviderRefund] = []
        self.as_of: int | None = None

    def balance_cents(self) -> int:
        return sum(
            MONEY_EFFECT[e.type] * e.amount_cents for e in self.events if self.as_of is None or e.created <= self.as_of
        )


class World:
    def __init__(self) -> None:
        self.provider = Provider()
        self.ledger = Ledger()
        self.ledger.conn.execute(
            "CREATE TABLE payments (id TEXT PRIMARY KEY, status TEXT NOT NULL, amount_cents INTEGER NOT NULL,"
            " provider_payment_id TEXT) STRICT"
        )
        self.ledger.conn.execute(
            "CREATE TABLE refunds (id TEXT PRIMARY KEY, payment_id TEXT NOT NULL, status TEXT NOT NULL,"
            " amount_cents INTEGER NOT NULL, provider_refund_id TEXT) STRICT"
        )

    def paid(self, payment_id: str = "pmt_000001", amount_cents: int = 2_500, at: int = 1) -> str:
        """A payment of ours that the provider charged and that we know succeeded. Returns the provider's id."""
        n = len(self.provider.payments) + 1
        charge = ProviderPayment(f"pay_{n:06d}", amount_cents, payment_id, at)
        self.provider.payments.append(charge)
        self._event(PAYMENT_SUCCEEDED, charge.id, amount_cents, at)
        self.ledger.conn.execute(
            "INSERT INTO payments VALUES (?, 'succeeded', ?, ?)", (payment_id, amount_cents, charge.id)
        )
        return charge.id

    def ours(
        self,
        refund_id: str,
        status: str,
        amount_cents: int,
        provider_refund_id: str | None = None,
        payment_id: str = "pmt_000001",
    ) -> None:
        self.ledger.conn.execute(
            "INSERT INTO refunds VALUES (?, ?, ?, ?, ?)",
            (refund_id, payment_id, status, amount_cents, provider_refund_id),
        )

    def refund(
        self, provider_payment_id: str, amount_cents: int, ours: str | None, at: int = 2, booked: bool = True
    ) -> str:
        """The provider refunds, naming our refund in its metadata; its webhook is booked unless booked=False."""
        refund = ProviderRefund(f"re_{len(self.provider.refunds) + 1:06d}", provider_payment_id, amount_cents, ours, at)
        self.provider.refunds.append(refund)
        self._event(REFUND_SUCCEEDED, provider_payment_id, amount_cents, at, booked)
        return refund.id

    def _event(self, kind: str, provider_payment_id: str, amount_cents: int, at: int, booked: bool = True) -> None:
        event = Event(f"evt_{len(self.provider.events) + 1:06d}", kind, provider_payment_id, amount_cents, at)
        self.provider.events.append(event)
        if booked:
            self.ledger.post(
                event_id=event.id,
                payment_id=provider_payment_id,
                description="test",
                lines=posting_lines(kind, amount_cents),
                posted_at=at,
                dedupe=True,
            )

    def check(self, cutoff: int | None = None):
        self.provider.as_of = cutoff
        p = self.provider
        return check_money(p, self.ledger.conn, p.payments, cutoff=cutoff, provider_refunds=p.refunds)


def only_the_fifth_fails(report) -> bool:
    return [i.name for i in report.invariants if not i.passed] == [FIFTH]


def test_refunds_that_agree_with_the_provider_pass():
    world = World()
    charge = world.paid()
    world.ours("rfd_000001", "succeeded", 1_000, world.refund(charge, 1_000, "rfd_000001"))
    world.ours("rfd_000002", "pending", 500)  # the provider was down: nothing refunded, nothing known
    world.ours("rfd_000003", "failed", 9_000)  # more than is left to refund: never sent
    report = world.check()
    assert report.passed, report
    assert [i.name for i in report.invariants] == [
        "posted_exactly_once",
        "ledger_balanced",
        "matches_provider",
        FOURTH,
        FIFTH,
    ]
    assert report.invariant(FIFTH).summary == (
        "all 3 of our refunds agree with the provider (1 refunded once, 2 never refunded),"
        " and no payment was refunded more than it was charged"
    )


def test_with_no_refunds_at_all_the_fifth_invariant_says_so():
    world = World()
    world.paid()
    assert world.check().invariant(FIFTH).summary == "no refunds yet, ours or the provider's"


def test_a_refund_whose_answer_was_lost_is_caught_and_only_by_the_fifth_invariant():
    """Known gap 1: the provider refunded, its answer never reached us, and nothing confirmed the refund."""
    world = World()
    world.refund(world.paid(), 1_000, "rfd_000001")
    world.ours("rfd_000001", "pending", 1_000)
    report = world.check()
    assert only_the_fifth_fails(report)  # the ledger booked the refund from its webhook: the books are right
    fifth = report.invariant(FIFTH)
    assert fifth.evidence == [
        {
            "refund_id": "rfd_000001",
            "payment_id": "pmt_000001",
            "status": "pending",
            "amount_cents": 1_000,
            "provider_refund_id": None,
            "provider_payment_id": "pay_000001",
            "provider_refunds": [{"id": "re_000001", "provider_payment_id": "pay_000001", "amount_cents": 1_000}],
            "problem": "pending but refunded",
        }
    ]
    assert fifth.summary == (
        "rfd_000001 ($10.00, a refund of pmt_000001) is pending in our records,"
        " but the provider refunded it (re_000001, $10.00)"
    )


def test_a_refund_recorded_as_failed_that_the_provider_made_is_caught():
    world = World()
    world.refund(world.paid(), 1_000, "rfd_000001")
    world.ours("rfd_000001", "failed", 1_000)
    [problem] = world.check().invariant(FIFTH).evidence
    assert problem["problem"] == "failed but refunded"


def test_a_succeeded_refund_the_provider_never_made_is_caught():
    world = World()
    world.paid()
    world.ours("rfd_000001", "succeeded", 1_000, "re_000001")
    report = world.check()
    assert only_the_fifth_fails(report)  # no refund, no booking: the ledger agrees with the provider
    assert report.invariant(FIFTH).summary == (
        "rfd_000001 ($10.00, a refund of pmt_000001) is succeeded in our records, but the provider has no refund for it"
    )


def test_a_refund_made_twice_passes_the_ledger_invariants_and_only_the_fifth_catches_it():
    """Two refunds at the provider for one refund of ours. The ledger books both faithfully; only our refunds
    table knows we asked for one."""
    world = World()
    charge = world.paid()
    first = world.refund(charge, 1_000, "rfd_000001")
    second = world.refund(charge, 1_000, "rfd_000001")
    world.ours("rfd_000001", "succeeded", 1_000, first)
    report = world.check()
    assert only_the_fifth_fails(report)
    assert report.money_off_cents == 0  # the ledger and the provider agree on every cent
    [problem] = report.invariant(FIFTH).evidence
    assert problem["problem"] == "refunded twice"
    assert [r["id"] for r in problem["provider_refunds"]] == [first, second]
    assert report.invariant(FIFTH).summary == (
        "rfd_000001 ($10.00, a refund of pmt_000001) was refunded 2 times at the provider (re_000001, re_000002)"
    )


def test_a_100x_refund_is_caught_only_by_the_fifth_invariant():
    """We asked to give back $10.00; the provider gave back $1,000.00. The ledger books what the provider did."""
    world = World()
    charge = world.paid(amount_cents=200_000)
    world.ours("rfd_000001", "succeeded", 1_000, world.refund(charge, 100_000, "rfd_000001"))
    report = world.check()
    assert only_the_fifth_fails(report)
    assert report.invariant(FIFTH).evidence[0]["problem"] == "amount differs"
    assert report.invariant(FIFTH).summary == (
        "rfd_000001 ($10.00, a refund of pmt_000001) was refunded $1,000.00 at the provider (re_000001)"
    )


def test_a_refund_of_another_customers_payment_is_caught():
    """Our refund is for pmt_000001, but the provider gave the money back on another payment: the wrong customer."""
    world = World()
    world.paid()
    other = world.paid("pmt_000002", 2_500)
    world.ours("rfd_000001", "succeeded", 1_000, world.refund(other, 1_000, "rfd_000001"))
    report = world.check()
    assert only_the_fifth_fails(report)
    assert report.invariant(FIFTH).evidence[0]["problem"] == "refunded another payment"
    assert report.invariant(FIFTH).summary == (
        "rfd_000001 ($10.00, a refund of pmt_000001) is for pay_000001, but the provider's refund re_000001 is for"
        " pay_000002"
    )


def test_a_refund_whose_payment_is_missing_from_our_records_is_still_judged():
    """The check must not depend on our bookkeeping being right: a refund with no payment row is not dropped."""
    world = World()
    made = world.refund(world.paid(), 1_000, "rfd_000001")
    world.ours("rfd_000001", "succeeded", 1_000, made, payment_id="pmt_000404")  # no such payment of ours
    fifth = world.check().invariant(FIFTH)
    assert fifth.evidence[0]["problem"] == "refunded another payment"
    assert fifth.summary == (
        "rfd_000001 ($10.00, a refund of pmt_000404) is for a payment with no provider id in our records, but the"
        " provider's refund re_000001 is for pay_000001"
    )


def test_a_refund_linked_to_another_provider_refund_is_caught():
    world = World()
    world.refund(world.paid(), 1_000, "rfd_000001")
    world.ours("rfd_000001", "succeeded", 1_000, "re_000099")
    fifth = world.check().invariant(FIFTH)
    assert fifth.evidence[0]["problem"] == "linked to another provider refund"
    assert fifth.summary == (
        "rfd_000001 ($10.00, a refund of pmt_000001) is linked to re_000099, but the provider's refund for it is"
        " re_000001"
    )


def test_a_stuck_refund_of_the_wrong_amount_reports_both_problems():
    """Reported as only "pending", an agent would confirm it: the wrong fix for a refund of the wrong amount."""
    world = World()
    world.refund(world.paid(amount_cents=200_000), 100_000, "rfd_000001")
    world.ours("rfd_000001", "pending", 1_000)
    fifth = world.check().invariant(FIFTH)
    assert fifth.evidence[0]["problem"] == "pending but refunded, amount differs"


def test_a_refund_whose_metadata_names_none_of_ours_is_matched_through_our_link():
    world = World()
    world.ours("rfd_000001", "succeeded", 1_000, world.refund(world.paid(), 1_000, None))
    fifth = world.check().invariant(FIFTH)
    assert [(e["refund_id"], e["problem"]) for e in fifth.evidence] == [
        ("rfd_000001", "not named in its provider refund's metadata")
    ]
    assert fifth.summary == (
        "rfd_000001 ($10.00, a refund of pmt_000001) is not named in the metadata of its provider refund re_000001"
    )


def test_a_refund_linked_to_a_provider_refund_the_provider_gives_to_another_refund_is_caught():
    world = World()
    made = world.refund(world.paid(), 1_000, "rfd_000002")  # the provider says this refund is for rfd_000002
    world.ours("rfd_000001", "succeeded", 1_000, made)  # but we linked it to rfd_000001
    world.ours("rfd_000002", "pending", 1_000)
    fifth = world.check().invariant(FIFTH)
    assert [(e["refund_id"], e["problem"]) for e in fifth.evidence] == [
        ("rfd_000001", "linked to another refund's provider refund"),
        ("rfd_000002", "pending but refunded"),
    ]
    assert fifth.summary.startswith(
        "rfd_000001 ($10.00, a refund of pmt_000001) is linked to re_000001, but the provider's metadata gives that"
        " refund to rfd_000002;"
    )


def test_a_refund_that_names_no_refund_of_ours_is_caught():
    world = World()
    charge = world.paid()
    world.refund(charge, 1_000, None)  # no id of ours in its metadata
    world.refund(charge, 500, "rfd_000404")  # an id we never issued
    fifth = world.check().invariant(FIFTH)
    assert [(e["refund_id"], e["problem"]) for e in fifth.evidence] == [
        (None, "refunded but not ours"),
        ("rfd_000404", "refunded but not ours"),
    ]
    assert fifth.summary == (
        "the provider refunded re_000001 ($10.00), and its metadata names no refund of ours;"
        " the provider refunded re_000002 ($5.00) for rfd_000404, which is not in our refunds table"
    )


def test_without_the_providers_refunds_the_check_has_four_invariants():
    world = World()
    world.refund(world.paid(), 1_000, "rfd_000001")
    world.ours("rfd_000001", "pending", 1_000)
    report = check_money(world.provider, world.ledger.conn, world.provider.payments)
    assert [i.name for i in report.invariants][-1] == FOURTH
    assert report.passed


# With a cutoff ---------------------------------------------------------------------------


def test_a_refund_after_the_cutoff_is_left_for_the_next_check():
    world = World()
    charge = world.paid(at=CUTOFF - 5)
    world.ours("rfd_000001", "succeeded", 1_000, world.refund(charge, 1_000, "rfd_000001", at=CUTOFF))
    world.ours("rfd_000002", "pending", 500)  # its answer and its webhook are still on their way
    world.refund(charge, 500, "rfd_000002", at=CUTOFF + 1, booked=False)
    world.refund(charge, 200, "rfd_000003", at=CUTOFF + 1, booked=False)  # ours, but newer than our snapshot
    report = world.check(cutoff=CUTOFF)
    assert report.passed, report
    assert report.left_for_next_check == {"provider_events": 2, "provider_charges": 0, "provider_refunds": 2}
    assert report.invariant(FIFTH).summary == (
        "all 1 of our refunds agree with the provider (1 refunded once, 0 never refunded),"
        " and no payment was refunded more than it was charged; 2 refunds newer than the cutoff left for the next check"
    )


def test_a_refund_up_to_the_cutoff_whose_answer_was_lost_is_still_caught():
    world = World()
    world.refund(world.paid(at=CUTOFF - 5), 1_000, "rfd_000001", at=CUTOFF)
    world.ours("rfd_000001", "pending", 1_000)
    report = world.check(cutoff=CUTOFF)
    assert only_the_fifth_fails(report)
    assert report.invariant(FIFTH).evidence[0]["problem"] == "pending but refunded"


def test_a_refund_made_twice_is_judged_whatever_the_cutoff():
    world = World()
    charge = world.paid(at=CUTOFF - 5)
    first = world.refund(charge, 1_000, "rfd_000001", at=CUTOFF)
    world.refund(charge, 1_000, "rfd_000001", at=CUTOFF + 1)
    world.ours("rfd_000001", "succeeded", 1_000, first)
    assert world.check(cutoff=CUTOFF).invariant(FIFTH).evidence[0]["problem"] == "refunded twice"


def test_with_nothing_after_the_cutoff_the_fifth_invariant_reads_as_without_one():
    world = World()
    world.refund(world.paid(), 1_000, "rfd_000001")
    world.ours("rfd_000001", "pending", 1_000)
    with_cutoff, without = world.check(cutoff=CUTOFF), world.check()
    assert with_cutoff.invariant(FIFTH) == without.invariant(FIFTH)
    assert with_cutoff.left_for_next_check["provider_refunds"] == 0


# Week 4, item 3: per payment, the provider never refunded more than it charged (known gap 1, decided 2026-10-14)


def test_two_refunds_that_each_agree_can_still_refund_more_than_was_charged_and_only_the_fifth_sees_it():
    """Before week 4 every invariant passed here. Each refund is ours, made once, for its amount, on its payment; the
    ledger books both as the provider sent them. Together they give back $30.00 of a $25.00 payment."""
    world = World()
    charge = world.paid()  # $25.00
    world.ours("rfd_000001", "succeeded", 1_500, world.refund(charge, 1_500, "rfd_000001"))
    world.ours("rfd_000002", "succeeded", 1_500, world.refund(charge, 1_500, "rfd_000002"))
    report = world.check()
    assert only_the_fifth_fails(report)
    assert report.invariant(FIFTH).evidence == [
        {
            "refund_id": None,
            "payment_id": "pmt_000001",
            "provider_payment_id": "pay_000001",
            "charged_cents": 2_500,
            "refunded_cents": 3_000,
            "provider_refunds": [
                {"id": "re_000001", "provider_payment_id": "pay_000001", "amount_cents": 1_500},
                {"id": "re_000002", "provider_payment_id": "pay_000001", "amount_cents": 1_500},
            ],
            "problem": OVER_REFUNDED,
        }
    ]
    assert report.invariant(FIFTH).summary == (
        "pmt_000001's charge pay_000001 ($25.00) was refunded $30.00 at the provider (re_000001, re_000002),"
        " more than it charged"
    )


def test_refunds_that_add_up_to_exactly_the_charge_pass():
    world = World()
    charge = world.paid()
    world.ours("rfd_000001", "succeeded", 1_000, world.refund(charge, 1_000, "rfd_000001"))
    world.ours("rfd_000002", "succeeded", 1_500, world.refund(charge, 1_500, "rfd_000002"))
    assert world.check().passed


def test_each_charge_is_judged_on_its_own_refunds():
    world = World()
    first, second = world.paid(), world.paid("pmt_000002", 1_000)
    world.ours("rfd_000001", "succeeded", 2_500, world.refund(first, 2_500, "rfd_000001"))
    world.ours("rfd_000002", "succeeded", 1_000, world.refund(second, 1_000, "rfd_000002"), payment_id="pmt_000002")
    world.ours("rfd_000003", "succeeded", 1, world.refund(second, 1, "rfd_000003"), payment_id="pmt_000002")
    [problem] = world.check().invariant(FIFTH).evidence
    assert (problem["provider_payment_id"], problem["charged_cents"], problem["refunded_cents"]) == (
        "pay_000002",
        1_000,
        1_001,
    )


def test_an_over_refund_is_judged_whatever_the_cutoff():
    """A charge's amount never changes and its refunds only add up: no timing explains a total over it. The refund
    that tips it over is newer than the cutoff, and is still left for the next check as a refund of ours."""
    world = World()
    charge = world.paid(at=CUTOFF - 5)
    world.ours("rfd_000001", "succeeded", 2_000, world.refund(charge, 2_000, "rfd_000001", at=CUTOFF))
    world.ours("rfd_000002", "pending", 1_000)
    world.refund(charge, 1_000, "rfd_000002", at=CUTOFF + 1, booked=False)
    report = world.check(cutoff=CUTOFF)
    assert [problem["problem"] for problem in report.invariant(FIFTH).evidence] == [OVER_REFUNDED]
    assert report.left_for_next_check["provider_refunds"] == 1


def test_a_refund_of_a_charge_the_provider_does_not_have_is_caught():
    world = World()
    world.paid()
    world.refund("pay_000404", 700, None)
    report = world.check()
    over = [item for item in report.invariant(FIFTH).evidence if item["problem"] == OVER_REFUNDED]
    assert [(item["provider_payment_id"], item["charged_cents"], item["payment_id"]) for item in over] == [
        ("pay_000404", None, None)
    ]
    assert report.invariant(FIFTH).summary.endswith(
        "the provider refunded $7.00 (re_000001) of pay_000404, a charge it does not have"
    )


def test_refunds_of_a_charge_not_read_yet_are_left_for_the_next_check_when_all_are_newer_than_the_cutoff():
    """Their charge may be newer than the provider's payments the check read. One up to the cutoff cannot be."""
    newer_only = World()
    newer_only.paid(at=CUTOFF - 5)
    newer_only.refund("pay_000404", 700, None, at=CUTOFF + 1, booked=False)
    assert newer_only.check(cutoff=CUTOFF).passed
    up_to_it = World()
    up_to_it.paid(at=CUTOFF - 5)
    up_to_it.refund("pay_000404", 700, None, at=CUTOFF, booked=False)
    up_to_it.refund("pay_000404", 300, None, at=CUTOFF + 1, booked=False)
    over = [i for i in up_to_it.check(cutoff=CUTOFF).invariant(FIFTH).evidence if i["problem"] == OVER_REFUNDED]
    assert [item["refunded_cents"] for item in over] == [1_000]


def test_a_charge_that_names_none_of_ours_is_tied_to_our_payment_through_our_refunds():
    world = World()
    world.paid()
    charge = ProviderPayment("pay_000002", 1_000, None, 1)  # its metadata names no payment of ours
    world.provider.payments.append(charge)
    world.ledger.conn.execute("INSERT INTO payments VALUES ('pmt_000002', 'succeeded', 1000, 'pay_000002')")
    world.ours("rfd_000001", "succeeded", 1_000, world.refund(charge.id, 1_000, "rfd_000001"), payment_id="pmt_000002")
    world.ours("rfd_000002", "succeeded", 500, world.refund(charge.id, 500, "rfd_000002"), payment_id="pmt_000002")
    over = [i for i in world.check().invariant(FIFTH).evidence if i["problem"] == OVER_REFUNDED]
    assert [item["payment_id"] for item in over] == ["pmt_000002"]


def test_a_charge_no_payment_of_ours_names_is_said_as_the_providers():
    world = World()
    world.paid()
    world.provider.payments.append(ProviderPayment("pay_000002", 1_000, None, 1))
    world.refund("pay_000002", 600, None)
    world.refund("pay_000002", 600, None)
    assert "the provider's charge pay_000002 ($10.00) was refunded $12.00 at the provider" in (
        world.check().invariant(FIFTH).summary
    )


def test_a_refund_that_names_no_payment_is_never_tied_to_one_of_ours_that_has_no_provider_id():
    world = World()
    world.paid()
    world.ours("rfd_000001", "pending", 500, payment_id="pmt_000404")  # its payment is not in our records
    world.provider.refunds.append(ProviderRefund("re_000009", None, 500, None, 1))  # it names no payment
    over = [i for i in world.check().invariant(FIFTH).evidence if i["problem"] == OVER_REFUNDED]
    assert [(item["provider_payment_id"], item["payment_id"]) for item in over] == [(None, None)]
    assert "the provider refunded $5.00 (re_000009) of no payment, a charge it does not have" in (
        world.check().invariant(FIFTH).summary
    )


def test_the_fifth_invariant_will_not_run_without_the_providers_payments():
    world = World()
    with pytest.raises(ValueError, match="needs the provider's payments too"):
        check_money(world.provider, world.ledger.conn, None, provider_refunds=world.provider.refunds)
