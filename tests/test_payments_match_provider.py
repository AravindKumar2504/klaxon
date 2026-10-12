"""The money check's fourth invariant: our payments table against the provider's payments.

Offline, on SQLite: the first piece's ledger, which books every charge the provider made correctly, plus a
payments table with the live schema's columns. The same SQL runs on PostgreSQL in tests/integration.
"""

from klaxon.checks.money import MONEY_EFFECT, ProviderPayment, check_money
from klaxon.patient.ledger import Ledger, posting_lines
from klaxon.patient.provider import PAYMENT_SUCCEEDED, Event

FOURTH = "payments_match_provider"


class Provider:
    """The provider's side: its charges, one payment.succeeded event each, and the balance they add up to."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.payments: list[ProviderPayment] = []

    def balance_cents(self) -> int:
        return sum(MONEY_EFFECT[e.type] * e.amount_cents for e in self.events)


class World:
    def __init__(self) -> None:
        self.provider = Provider()
        self.ledger = Ledger()
        self.ledger.conn.execute(
            "CREATE TABLE payments (id TEXT PRIMARY KEY, status TEXT NOT NULL, amount_cents INTEGER NOT NULL,"
            " provider_payment_id TEXT) STRICT"
        )

    def ours(self, payment_id: str, status: str, amount_cents: int, provider_payment_id: str | None = None) -> None:
        self.ledger.conn.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?)", (payment_id, status, amount_cents, provider_payment_id)
        )

    def charge(self, amount_cents: int, ours: str | None) -> str:
        """The provider charges, and its webhook is booked correctly: the ledger always matches the provider."""
        n = len(self.provider.payments) + 1
        payment = ProviderPayment(f"pay_{n:06d}", amount_cents, ours)
        event = Event(f"evt_{n:06d}", PAYMENT_SUCCEEDED, payment.id, amount_cents, n)
        self.provider.payments.append(payment)
        self.provider.events.append(event)
        self.ledger.post(
            event_id=event.id,
            payment_id=payment.id,
            description="test",
            lines=posting_lines(event.type, amount_cents),
            posted_at=n,
            dedupe=True,
        )
        return payment.id

    def check(self):
        return check_money(self.provider, self.ledger.conn, self.provider.payments)


def ledger_invariants_pass(report) -> bool:
    return all(i.passed for i in report.invariants if i.name != FOURTH)


def test_payments_that_agree_with_the_provider_pass():
    world = World()
    for n, amount in enumerate((2_500, 4_900, 1_250), start=1):
        world.ours(f"pmt_00000{n}", "succeeded", amount, world.charge(amount, f"pmt_00000{n}"))
    world.ours("pmt_000004", "pending", 700)  # the provider was down: nothing charged, nothing known
    world.ours("pmt_000005", "failed", 900)  # the provider refused it: nothing charged
    report = world.check()
    assert report.passed, report
    assert [i.name for i in report.invariants][-1] == FOURTH
    assert report.invariant(FOURTH).summary == (
        "all 5 of our payments agree with the provider (3 charged once, 2 never charged)"
    )


def test_a_charged_payment_left_pending_is_caught_and_only_by_the_fourth_invariant():
    """The week 1 gap: the provider charged, its answer never reached us, and nothing ever confirmed it."""
    world = World()
    world.ours("pmt_000001", "succeeded", 2_500, world.charge(2_500, "pmt_000001"))
    world.charge(6_968, "pmt_000002")
    world.ours("pmt_000002", "pending", 6_968)
    report = world.check()
    assert ledger_invariants_pass(report)  # the ledger booked the charge from its webhook: the books are right
    fourth = report.invariant(FOURTH)
    assert not fourth.passed
    assert fourth.evidence == [
        {
            "payment_id": "pmt_000002",
            "status": "pending",
            "amount_cents": 6_968,
            "provider_payment_id": None,
            "provider_charges": [{"id": "pay_000002", "amount_cents": 6_968}],
            "problem": "pending but charged",
        }
    ]
    assert fourth.summary == (
        "pmt_000002 ($69.68) is pending in our records, but the provider charged it (pay_000002, $69.68)"
    )


def test_a_charged_payment_recorded_as_failed_is_caught():
    world = World()
    world.charge(2_500, "pmt_000001")
    world.ours("pmt_000001", "failed", 2_500)
    [problem] = world.check().invariant(FOURTH).evidence
    assert problem["problem"] == "failed but charged"


def test_a_succeeded_payment_the_provider_never_charged_is_caught():
    world = World()
    world.ours("pmt_000001", "succeeded", 2_500, "pay_000001")
    report = world.check()
    assert ledger_invariants_pass(report)  # no charge, no booking: the ledger agrees with the provider
    fourth = report.invariant(FOURTH)
    assert fourth.evidence[0]["problem"] == "succeeded but never charged"
    assert fourth.summary == "pmt_000001 ($25.00) is succeeded in our records, but the provider has no charge for it"


def test_a_true_double_charge_passes_the_ledger_invariants_and_only_the_fourth_catches_it():
    """Two charges for one payment of ours (a retry sent without the same idempotency key). The ledger books both
    charges faithfully, so it matches the provider. Only our payments table knows we asked for one."""
    world = World()
    first = world.charge(2_500, "pmt_000001")
    second = world.charge(2_500, "pmt_000001")
    world.ours("pmt_000001", "succeeded", 2_500, first)
    report = world.check()
    assert ledger_invariants_pass(report)
    assert report.money_off_cents == 0  # the ledger and the provider agree on every cent
    fourth = report.invariant(FOURTH)
    assert not fourth.passed
    [problem] = fourth.evidence
    assert problem["problem"] == "charged twice"
    assert [c["id"] for c in problem["provider_charges"]] == [first, second]
    assert fourth.summary == "pmt_000001 ($25.00) was charged 2 times at the provider (pay_000001, pay_000002)"


def test_a_100x_money_unit_error_is_caught_only_by_the_fourth_invariant():
    """We asked for $5.00; a bad deploy sent 500 dollars' worth of cents. The ledger books what the provider took."""
    world = World()
    world.ours("pmt_000001", "succeeded", 500, world.charge(50_000, "pmt_000001"))
    report = world.check()
    assert ledger_invariants_pass(report)
    fourth = report.invariant(FOURTH)
    assert fourth.evidence[0]["problem"] == "amount differs"
    assert fourth.summary == "pmt_000001 ($5.00) was charged $500.00 at the provider (pay_000001)"


def test_a_payment_linked_to_another_charge_is_caught():
    world = World()
    world.charge(2_500, "pmt_000001")
    world.ours("pmt_000001", "succeeded", 2_500, "pay_000099")
    fourth = world.check().invariant(FOURTH)
    assert fourth.evidence[0]["problem"] == "linked to another charge"
    assert (
        fourth.summary == "pmt_000001 ($25.00) is linked to pay_000099, but the provider's charge for it is pay_000001"
    )


def test_a_stuck_payment_charged_the_wrong_amount_reports_both_problems():
    """Reported as only "pending", an agent would confirm it: the wrong fix for a charge of the wrong amount."""
    world = World()
    world.charge(50_000, "pmt_000001")
    world.ours("pmt_000001", "pending", 500)
    fourth = world.check().invariant(FOURTH)
    assert fourth.evidence[0]["problem"] == "pending but charged, amount differs"
    assert fourth.summary == (
        "pmt_000001 ($5.00) is pending in our records, but the provider charged it (pay_000001, $500.00)"
    )


def test_a_charge_whose_metadata_names_none_of_ours_is_matched_through_our_link():
    """Charged once and linked, but unnamed: the confirmer could never match its webhook. Not "never charged"."""
    world = World()
    world.ours("pmt_000001", "succeeded", 2_500, world.charge(2_500, None))
    fourth = world.check().invariant(FOURTH)
    assert [(e["payment_id"], e["problem"]) for e in fourth.evidence] == [
        ("pmt_000001", "not named in its charge's metadata")
    ]
    assert fourth.summary == "pmt_000001 ($25.00) is not named in the metadata of its charge pay_000001"


def test_a_payment_linked_to_a_charge_the_provider_gives_to_another_payment_is_caught():
    world = World()
    charge = world.charge(2_500, "pmt_000002")  # the provider says this charge is for pmt_000002
    world.ours("pmt_000001", "succeeded", 2_500, charge)  # but we linked it to pmt_000001
    world.ours("pmt_000002", "pending", 2_500)
    fourth = world.check().invariant(FOURTH)
    assert [(e["payment_id"], e["problem"]) for e in fourth.evidence] == [
        ("pmt_000001", "linked to another payment's charge"),
        ("pmt_000002", "pending but charged"),
    ]
    assert fourth.summary.startswith(
        "pmt_000001 ($25.00) is linked to pay_000001, but the provider's metadata gives that charge to pmt_000002;"
    )


def test_a_charge_that_names_no_payment_of_ours_is_caught():
    world = World()
    world.charge(1_000, None)  # no id of ours in its metadata
    world.charge(2_000, "pmt_000404")  # an id we never issued
    fourth = world.check().invariant(FOURTH)
    assert [(e["payment_id"], e["problem"]) for e in fourth.evidence] == [
        (None, "charged but not ours"),
        ("pmt_000404", "charged but not ours"),
    ]
    assert fourth.summary == (
        "the provider charged pay_000001 ($10.00), and its metadata names no payment of ours;"
        " the provider charged pay_000002 ($20.00) for pmt_000404, which is not in our payments table"
    )


def test_every_problem_is_reported_not_only_the_first():
    world = World()
    world.ours("pmt_000001", "pending", 100, None)
    world.charge(100, "pmt_000001")
    world.ours("pmt_000002", "succeeded", 200, "pay_000777")
    assert len(world.check().invariant(FOURTH).evidence) == 2


def test_without_the_providers_payments_the_check_is_the_first_pieces_three_invariants():
    world = World()
    world.ours("pmt_000001", "pending", 2_500)
    world.charge(2_500, "pmt_000001")
    report = check_money(world.provider, world.ledger.conn)
    assert [i.name for i in report.invariants] == ["posted_exactly_once", "ledger_balanced", "matches_provider"]
    assert report.passed
