"""The money check with a cutoff: under live traffic, only what the provider did up to the cutoff is judged.

Offline, on SQLite: the first piece's ledger plus a payments table with the live schema's columns, like
test_payments_match_provider. The provider stamps records with whole seconds; the cutoff is one of them.
A record stamped after the cutoff may not have reached our side yet, so it is left for the next check:
that is the only thing the cutoff may change. Everything up to the cutoff is judged exactly as without one.
"""

from klaxon.checks.money import MONEY_EFFECT, ProviderPayment, check_money
from klaxon.patient.ledger import Ledger, posting_lines
from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED, Event

CUTOFF = 1_000
FIRST, FOURTH = "posted_exactly_once", "payments_match_provider"


class Provider:
    """The provider's records, read after our snapshot, with its balance as of the cutoff."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.payments: list[ProviderPayment] = []
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
        self._events = 0

    def ours(self, payment_id: str, status: str, amount_cents: int, provider_payment_id: str | None = None) -> None:
        self.ledger.conn.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?)", (payment_id, status, amount_cents, provider_payment_id)
        )

    def event(self, kind: str, payment_id: str, amount_cents: int, at: int, booked: int = 1) -> Event:
        """The provider records an event at `at`; our ledger has booked it `booked` times so far."""
        self._events += 1
        event = Event(f"evt_{self._events:06d}", kind, payment_id, amount_cents, at)
        self.provider.events.append(event)
        for _ in range(booked):
            self.book(event.id, payment_id, kind, amount_cents)
        return event

    def book(self, event_id: str, payment_id: str, kind: str, amount_cents: int) -> None:
        self.ledger.post(
            event_id=event_id,
            payment_id=payment_id,
            description="test",
            lines=posting_lines(kind, amount_cents),
            posted_at=0,
            dedupe=False,
        )

    def charge(self, ours: str | None, amount_cents: int, at: int, booked: int = 1) -> str:
        """The provider charges at `at`, naming our payment in its metadata."""
        payment = ProviderPayment(f"pay_{len(self.provider.payments) + 1:06d}", amount_cents, ours, at)
        self.provider.payments.append(payment)
        self.event(PAYMENT_SUCCEEDED, payment.id, amount_cents, at, booked)
        return payment.id

    def check(self, cutoff: int | None = CUTOFF):
        self.provider.as_of = cutoff
        return check_money(self.provider, self.ledger.conn, self.provider.payments, cutoff=cutoff)


def busy_world() -> World:
    """Three payments settled before the cutoff, and traffic still arriving after it."""
    world = World()
    for n, amount in enumerate((2_500, 4_900, 1_250), start=1):
        world.ours(f"pmt_00000{n}", "succeeded", amount, world.charge(f"pmt_00000{n}", amount, at=CUTOFF - 10 + n))
    return world


def test_records_after_the_cutoff_are_left_for_the_next_check_and_the_rest_passes():
    world = busy_world()
    # After the cutoff: a charge whose answer and webhook are both still on their way...
    world.ours("pmt_000004", "pending", 700)
    world.charge("pmt_000004", 700, at=CUTOFF + 1, booked=0)
    # ...and one whose answer came back and whose webhook was already booked.
    world.ours("pmt_000005", "succeeded", 900, world.charge("pmt_000005", 900, at=CUTOFF + 1))
    report = world.check()
    assert report.passed, report
    assert (report.ledger_clearing_cents, report.provider_balance_cents) == (8_650, 8_650)  # both as of the cutoff
    assert report.cutoff == CUTOFF
    assert report.left_for_next_check == {"provider_events": 2, "provider_charges": 2}
    assert report.invariant(FIRST).summary == (
        "all 3 provider events up to the cutoff posted exactly once (2 newer ones are left for the next check)"
    )
    assert report.invariant(FOURTH).summary == (
        "all 3 of our payments agree with the provider (3 charged once, 0 never charged);"
        " 2 charges newer than the cutoff left for the next check"
    )
    assert report.invariant("matches_provider").summary == "ledger and provider both say $86.50"


def test_without_the_cutoff_the_same_traffic_looks_like_lost_money():
    """What known gap 3 looked like: the in-flight payment reads as lost, and as charged but still pending."""
    world = busy_world()
    world.ours("pmt_000004", "pending", 700)
    world.charge("pmt_000004", 700, at=CUTOFF + 1, booked=0)
    report = world.check(cutoff=None)
    assert [i.name for i in report.invariants if not i.passed] == [FIRST, "matches_provider", FOURTH]
    assert report.invariant(FIRST).evidence[0]["problem"] == "lost"


def test_with_nothing_after_the_cutoff_every_summary_reads_as_without_one():
    """The quiet demos print the same results with the cutoff as before it."""
    world = busy_world()
    world.ours("pmt_000004", "pending", 700)  # never charged
    world.event(REFUND_SUCCEEDED, world.provider.payments[0].id, 1_000, at=CUTOFF)
    with_cutoff, without = world.check(), world.check(cutoff=None)
    assert [(i.name, i.passed, i.summary) for i in with_cutoff.invariants] == [
        (i.name, i.passed, i.summary) for i in without.invariants
    ]
    assert with_cutoff.left_for_next_check == {"provider_events": 0, "provider_charges": 0}


# Invariant 1 and 3: what the ledger booked ---------------------------------------------


def test_an_event_up_to_the_cutoff_that_was_never_booked_is_still_lost():
    world = busy_world()
    world.ours("pmt_000004", "succeeded", 700, world.charge("pmt_000004", 700, at=CUTOFF, booked=0))
    report = world.check()
    [lost] = report.invariant(FIRST).evidence
    assert (lost["event_id"], lost["problem"]) == ("evt_000004", "lost")
    assert report.money_off_cents == -700


def test_an_event_booked_twice_up_to_the_cutoff_is_still_counted_twice():
    world = busy_world()
    world.ours("pmt_000004", "succeeded", 700, world.charge("pmt_000004", 700, at=CUTOFF, booked=2))
    report = world.check()
    [twice] = report.invariant(FIRST).evidence
    assert (twice["event_id"], twice["problem"]) == ("evt_000004", "counted twice")
    assert report.money_off_cents == 700


def test_an_event_booked_twice_after_the_cutoff_is_caught_by_the_next_check():
    """The price of the cutoff: a problem in a record newer than the cutoff shows one check later."""
    world = busy_world()
    world.ours("pmt_000004", "succeeded", 700, world.charge("pmt_000004", 700, at=CUTOFF + 1, booked=2))
    assert world.check().passed
    assert not world.check(cutoff=CUTOFF + 1).invariant(FIRST).passed  # the next check's cutoff covers it


def test_a_posting_the_provider_never_sent_is_caught_whatever_the_cutoff():
    """Our snapshot is taken before the provider is read: an event we booked is already in its records."""
    world = busy_world()
    world.book("evt_forged", "pay_000001", PAYMENT_SUCCEEDED, 5_000)
    report = world.check()
    [forged] = report.invariant(FIRST).evidence
    assert (forged["event_id"], forged["problem"]) == ("evt_forged", "posted, but the provider never sent it")
    assert report.money_off_cents == 5_000  # and it counts in the ledger's total


def test_a_refund_after_the_cutoff_is_left_out_on_both_sides():
    world = busy_world()
    world.event(REFUND_SUCCEEDED, world.provider.payments[0].id, 1_000, at=CUTOFF + 1)  # booked, newer
    report = world.check()
    assert report.passed, report
    assert report.ledger_clearing_cents == report.provider_balance_cents == 8_650  # the refund is not in either


def test_a_refund_up_to_the_cutoff_that_was_never_booked_is_caught():
    world = busy_world()
    world.event(REFUND_SUCCEEDED, world.provider.payments[0].id, 1_000, at=CUTOFF, booked=0)
    report = world.check()
    assert report.invariant(FIRST).evidence[0]["problem"] == "lost"
    assert report.money_off_cents == 1_000  # our books claim money the provider gave back


# Invariant 4: our payments table ------------------------------------------------------


def test_a_payment_charged_up_to_the_cutoff_and_still_pending_is_caught():
    world = busy_world()
    world.ours("pmt_000004", "pending", 700)
    world.charge("pmt_000004", 700, at=CUTOFF)
    [stuck] = world.check().invariant(FOURTH).evidence
    assert (stuck["payment_id"], stuck["problem"]) == ("pmt_000004", "pending but charged")


def test_a_payment_whose_charge_is_after_the_cutoff_may_still_be_pending():
    world = busy_world()
    world.ours("pmt_000004", "pending", 700)
    world.charge("pmt_000004", 700, at=CUTOFF + 1)
    assert world.check().invariant(FOURTH).passed


def test_a_double_charge_is_caught_even_when_one_of_the_charges_is_after_the_cutoff():
    """Two charges for one payment are never a matter of timing."""
    world = busy_world()
    first = world.charge("pmt_000004", 700, at=CUTOFF)
    world.charge("pmt_000004", 700, at=CUTOFF + 1)
    world.ours("pmt_000004", "succeeded", 700, first)
    [twice] = world.check().invariant(FOURTH).evidence
    assert (twice["payment_id"], twice["problem"]) == ("pmt_000004", "charged twice")


def test_a_succeeded_payment_with_no_charge_at_all_is_caught_whatever_the_cutoff():
    """Our snapshot is taken before the provider is read: a real charge would already be in its records."""
    world = busy_world()
    world.ours("pmt_000004", "succeeded", 700, "pay_000099")
    [ghost] = world.check().invariant(FOURTH).evidence
    assert (ghost["payment_id"], ghost["problem"]) == ("pmt_000004", "succeeded but never charged")


def test_a_wrong_amount_up_to_the_cutoff_is_caught():
    world = busy_world()
    world.ours("pmt_000004", "succeeded", 700, world.charge("pmt_000004", 70_000, at=CUTOFF))
    [wrong] = world.check().invariant(FOURTH).evidence
    assert (wrong["payment_id"], wrong["problem"]) == ("pmt_000004", "amount differs")


def test_a_charge_that_is_not_ours_is_caught_up_to_the_cutoff_and_left_for_later_after_it():
    world = busy_world()
    world.charge(None, 3_000, at=CUTOFF + 1)  # its payment row may not be in our snapshot yet
    later = world.check()
    assert later.invariant(FOURTH).summary == (
        "all 3 of our payments agree with the provider (3 charged once, 0 never charged);"
        " 1 charge newer than the cutoff left for the next check"
    )
    assert later.left_for_next_check["provider_charges"] == 1
    world.charge(None, 4_000, at=CUTOFF)
    [stranger] = world.check().invariant(FOURTH).evidence
    assert (stranger["problem"], stranger["provider_charges"]) == (
        "charged but not ours",
        [{"id": "pay_000005", "amount_cents": 4_000}],
    )


def test_a_charge_with_no_time_is_judged():
    """A record with no time cannot be shown to be newer than the cutoff, so it is not left out."""
    world = busy_world()
    world.ours("pmt_000004", "pending", 700)
    world.provider.payments.append(ProviderPayment("pay_000004", 700, "pmt_000004"))
    [stuck] = world.check().invariant(FOURTH).evidence
    assert stuck["problem"] == "pending but charged"
