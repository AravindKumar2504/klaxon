"""Verification after an approved action, offline: the money check's findings judged against the check right after
the action, health from the metric rules and the containers, and the verdict in plain sentences.

Everything here is made up for the tests: the money checks' reports, the provider's records, the rules' readings
and docker's containers. The first piece's own ledger (SQLite, klaxon.patient.ledger) plays the books in one test,
to show for real why a correct rollback can still see an old event booked again."""

import httpx
import pytest

from klaxon import alerts
from klaxon.agent import verify
from klaxon.agent.verify import BEFORE, NEW, SURFACED, Finding, Reading
from klaxon.checks import live
from klaxon.checks.money import InvariantResult, MoneyReport, ProviderPayment, ProviderRefund, check_money
from klaxon.patient.ledger import Ledger, WebhookEndpoint
from klaxon.patient.provider import PAYMENT_SUCCEEDED, Event, FakeProvider
from tests.conftest import SECRET
from tests.test_alerts import HEALTHY, PASSED, Readings, rules

BOUNDARY = 1_792_400_000  # the cutoff of the made-up money check right after the action (provider's clock)
EARLY, LATE = BOUNDARY - 60, BOUNDARY + 30  # records made before the action, and after it


def event(n, created, payment=None, amount=2_500):
    return Event(f"evt_{n:04d}", PAYMENT_SUCCEEDED, payment or f"pay_{n:04d}", amount, created)


def posted(e, times=2):
    """posted_exactly_once's evidence for one event, as klaxon.checks.money writes it."""
    problem = "lost" if times == 0 else "counted twice" if times == 2 else f"counted {times} times"
    return {
        "event_id": e.id,
        "type": e.type,
        "payment_id": e.payment_id,
        "amount_cents": e.amount_cents,
        "times_posted": times,
        "expected": 1,
        "problem": problem,
        "transaction_ids": list(range(1, times + 1)),
    }


def differs(payment, cents):
    return {"scope": payment, "ledger_cents": 2 * cents, "provider_cents": cents, "difference_cents": cents}


def total(cents):
    return {"scope": verify.TOTAL, "ledger_cents": cents, "provider_cents": 0, "difference_cents": cents}


class Records:
    """The provider's records, as klaxon.checks.live.ProviderAPI holds them: lists, each record with its stamp."""

    def __init__(self, events=(), payments=(), refunds=()):
        self.events, self.payments, self.refunds = list(events), list(payments), list(refunds)


def report(**failed):
    """A money report whose named invariants failed with this evidence; the others passed."""
    names = ("posted_exactly_once", "ledger_balanced", "matches_provider", "payments_match_provider")
    invariants = [InvariantResult(name, name not in failed, "made up", list(failed.get(name, []))) for name in names]
    off = sum(item["difference_cents"] for item in failed.get("matches_provider", []) if item["scope"] == verify.TOTAL)
    return MoneyReport(not failed, 0, 0, off, invariants)


def check(cutoff, records=None, **failed):
    """A made-up money check: what check_live returns, without the stack."""
    return lambda wait, timeout_seconds: live.LiveCheck(cutoff, None, records or Records(), report(**failed))


def reading(cutoff, records=None, **failed):
    return verify.read(check(cutoff, records, **failed), 1.0, lambda: BOUNDARY)


# Reading a money check


def test_a_failed_check_keeps_each_finding_with_the_time_of_what_it_is_about():
    old, late = event(1, EARLY), event(2, LATE)
    found = reading(BOUNDARY, Records([old, late]), posted_exactly_once=[posted(old), posted(late, 0)])
    assert (found.outcome, found.cutoff, found.at_utc) == ("failed", BOUNDARY, "2026-10-19T08:53:20Z")
    assert found.findings == (
        Finding("posted_exactly_once", verify.COUNTED_TWICE, "event evt_0001 posted 2 times", EARLY, "pay_0001"),
        Finding("posted_exactly_once", verify.LOST, "event evt_0002 posted 0 times", LATE, "pay_0002"),
    )
    assert found.counts() == {verify.COUNTED_TWICE: 1, verify.LOST: 1}


def test_charges_and_refunds_are_dated_by_the_providers_records_and_one_without_a_stamp_is_not():
    charges = [ProviderPayment("ch_1", 500, "pmt_000001", EARLY), ProviderPayment("ch_2", 500, "pmt_000002", None)]
    refunds = [ProviderRefund("re_1", "ch_1", 100, "rfd_000001", LATE)]
    payments = [
        {
            "payment_id": "pmt_000001",
            "provider_charges": [{"id": "ch_1", "amount_cents": 500}],
            "problem": "amount differs",
        },
        {
            "payment_id": "pmt_000002",
            "provider_charges": [{"id": "ch_2", "amount_cents": 500}],
            "problem": "amount differs",
        },
        {"payment_id": "pmt_000003", "provider_charges": [], "problem": "succeeded but never charged"},
    ]
    refunded = [{"refund_id": "rfd_000001", "provider_refunds": [{"id": "re_1"}], "problem": "refunded twice"}]
    found = verify.findings_of(
        MoneyReport(
            False,
            0,
            0,
            0,
            [
                InvariantResult("payments_match_provider", False, "made up", payments),
                InvariantResult("refunds_match_provider", False, "made up", refunded),
            ],
        ),
        Records(payments=charges, refunds=refunds),
    )
    assert [(f.kind, f.created) for f in found] == [
        (verify.PAYMENT, EARLY),
        (verify.PAYMENT, None),  # its charge has no stamp: it cannot be dated
        (verify.PAYMENT, None),  # no charge at all
        (verify.REFUND, LATE),
    ]


def test_a_finding_about_several_records_is_dated_only_when_each_has_a_stamp():
    made = {"ch_1": EARLY, "ch_2": None, "ch_3": LATE}
    assert verify._newest(["ch_1", "ch_3"], made) == LATE  # the newest of them
    assert verify._newest(["ch_1", "ch_2"], made) is None  # one without a stamp
    assert verify._newest(["ch_1", "ch_9"], made) is None  # one the provider's records do not hold
    assert verify._newest([], made) is None


def test_a_passed_check_has_no_findings():
    found = reading(BOUNDARY)
    assert (found.outcome, found.findings, found.money_off_cents) == ("passed", (), 0)


def test_a_check_that_cannot_settle_is_a_reading_with_its_cutoff_and_what_is_behind():
    def stalled(wait, timeout_seconds):
        raise live.NotSettled(live.Settling(False, 60.0, BOUNDARY, 0, {"ledger": 271, "payment-confirmer": 0}, {}))

    found = verify.read(stalled, 60.0, lambda: BOUNDARY)
    assert (found.outcome, found.cutoff, found.behind) == (
        "could not settle",
        BOUNDARY,
        {"ledger": 271, "payment-confirmer": 0},
    )
    assert (
        found.why
        == "not settled after 60.0 s: messages up to its cutoff not handled yet: ledger 271, payment-confirmer 0"
    )


@pytest.mark.parametrize(
    "error, why",
    [
        (live.ClockProblem("x"), "the provider's clock and its records disagree"),
        (ConnectionRefusedError("x"), "the money check could not run (ConnectionRefusedError)"),
    ],
)
def test_a_check_that_cannot_run_is_a_reading_too_and_never_raises(error, why):
    def broken(wait, timeout_seconds):
        raise error

    found = verify.read(broken, 60.0, lambda: BOUNDARY)
    assert (found.outcome, found.cutoff, found.findings, found.why) == ("could not run", None, (), why)


def test_a_reading_saved_in_the_evidence_reads_back_the_same():
    old = event(1, EARLY)
    found = reading(BOUNDARY, Records([old]), posted_exactly_once=[posted(old)], matches_provider=[total(2_500)])
    assert Reading.from_dict(found.to_dict()) == found


# The money: what is new since the action


def test_damage_the_check_right_after_the_action_had_is_damage_from_before_it():
    old = event(1, EARLY)
    failed = {"posted_exactly_once": [posted(old)], "matches_provider": [differs("pay_0001", 2_500), total(2_500)]}
    money = verify.judge(reading(BOUNDARY, Records([old]), **failed), reading(LATE, Records([old]), **failed))
    assert (money.cause_removed, money.right) == (True, False)
    assert money.judged == {
        BEFORE: [
            "event evt_0001 posted 2 times",
            "payment pay_0001: books off by 2500 cents",
            "all payments: books off by 2500 cents",
        ],
        SURFACED: [],
        NEW: [],
    }


def test_an_old_event_booked_again_after_the_action_is_old_damage_seen_later():
    """The naive handler keeps no record of the events it books, so after a rollback, a webhook the provider sends
    again for one of them is booked once more: a finding the check right after the action did not have, about an
    event made before it. Damage from before the action, not a sign the cause is still there."""
    old, again = event(1, EARLY), event(2, EARLY)
    before = reading(BOUNDARY, Records([old, again]), posted_exactly_once=[posted(old)])
    later = reading(
        LATE,
        Records([old, again]),
        posted_exactly_once=[posted(old), posted(again)],
        matches_provider=[differs("pay_0002", 2_500), total(5_000)],
    )
    money = verify.judge(before, later)
    assert money.cause_removed is True
    assert money.judged[SURFACED] == [
        "event evt_0002 posted 2 times",
        "payment pay_0002: books off by 2500 cents",
        "all payments: books off by 5000 cents",
    ]


def test_damage_about_records_made_after_the_action_is_new_and_the_cause_is_not_removed():
    old, late = event(1, EARLY), event(2, LATE)
    before = reading(BOUNDARY, Records([old]), posted_exactly_once=[posted(old)])
    later = reading(
        LATE,
        Records([old, late]),
        posted_exactly_once=[posted(old), posted(late)],
        matches_provider=[differs("pay_0002", 2_500), total(5_000)],
    )
    money = verify.judge(before, later)
    assert money.cause_removed is False
    assert money.judged[NEW] == [
        "event evt_0002 posted 2 times",
        "payment pay_0002: books off by 2500 cents",
        "all payments: books off by 5000 cents",
    ]
    assert money.judged[BEFORE] == ["event evt_0001 posted 2 times"]


def test_an_event_at_the_cutoffs_own_second_counts_as_made_before_the_action():
    edge = event(1, BOUNDARY)
    money = verify.judge(reading(BOUNDARY), reading(LATE, Records([edge]), posted_exactly_once=[posted(edge)]))
    assert money.judged[SURFACED] == ["event evt_0001 posted 2 times"]


@pytest.mark.parametrize(
    "failed",
    [
        {"posted_exactly_once": [{"event_id": "evt_9999", "times_posted": 1, "expected": 0, "problem": "never sent"}]},
        {"ledger_balanced": [{"transaction_id": 7, "lines": 1, "debits_cents": 100, "credits_cents": 0}]},
        {
            "payments_match_provider": [
                {"payment_id": "pmt_000001", "provider_charges": [], "problem": "succeeded but never charged"}
            ]
        },
        {"posted_exactly_once": [posted(event(5, EARLY))]},  # the provider's records do not hold the event
        {"matches_provider": [differs("pay_0007", 900)]},  # a payment's books, and no finding about its events
        {"matches_provider": [total(900)]},  # the total, and nothing else to follow
    ],
    ids=["never sent", "unbalanced", "no charge", "an event not in the records", "a payment alone", "the total alone"],
)
def test_a_finding_that_cannot_be_dated_counts_as_new(failed):
    # Klaxon says the cause is removed only when it can show it.
    money = verify.judge(reading(BOUNDARY), reading(LATE, **failed))
    assert money.cause_removed is False and len(money.judged[NEW]) == 1


def test_a_payments_books_follow_the_findings_about_its_own_events():
    old, late = event(1, EARLY, payment="pay_0001"), event(2, LATE, payment="pay_0002")
    later = reading(
        LATE,
        Records([old, late]),
        posted_exactly_once=[posted(old), posted(late)],
        matches_provider=[differs("pay_0001", 2_500), differs("pay_0002", 2_500)],
    )
    money = verify.judge(reading(BOUNDARY), later)
    assert money.judged[SURFACED] == ["event evt_0001 posted 2 times", "payment pay_0001: books off by 2500 cents"]
    assert money.judged[NEW] == ["event evt_0002 posted 2 times", "payment pay_0002: books off by 2500 cents"]


def test_a_later_check_that_passed_means_the_money_is_right_and_the_cause_removed():
    money = verify.judge(reading(BOUNDARY, posted_exactly_once=[posted(event(1, EARLY))]), reading(LATE))
    assert (money.cause_removed, money.right, money.why) == (True, True, None)


@pytest.mark.parametrize("outcome", ["could not settle", "could not run"])
def test_a_later_check_with_no_verdict_leaves_both_questions_open(outcome):
    later = Reading("2026-10-20T03:07:10Z", outcome, None, why="made up")
    money = verify.judge(reading(BOUNDARY), later)
    assert (money.cause_removed, money.right) == (None, None)
    assert money.why == f"the money check after the action {outcome}: made up"


def test_with_no_check_right_after_the_action_whether_the_cause_is_removed_is_not_known():
    nothing = Reading("2026-10-20T03:06:40Z", "could not run", None, why="the money check could not run (OSError)")
    money = verify.judge(
        nothing, reading(LATE, Records([event(2, LATE)]), posted_exactly_once=[posted(event(2, LATE))])
    )
    assert (money.cause_removed, money.right) == (None, False)
    assert "right after the action could not run" in money.why


def test_a_check_right_after_the_action_that_could_not_settle_still_dates_what_came_later():
    stalled = Reading("2026-10-20T03:06:40Z", "could not settle", BOUNDARY, behind={"ledger": 300})
    old, late = event(1, EARLY), event(2, LATE)
    later = reading(LATE, Records([old, late]), posted_exactly_once=[posted(old), posted(late)])
    money = verify.judge(stalled, later)
    assert money.judged[SURFACED] == ["event evt_0001 posted 2 times"] and money.judged[NEW] == [
        "event evt_0002 posted 2 times"
    ]


def test_with_the_first_pieces_own_ledger_a_redelivery_after_the_rollback_is_booked_again_and_judged_old():
    """The real ledger code (SQLite), made-up deliveries: the naive handler books evt_0002 twice and evt_0001 once,
    with no record of either; the "rollback" hands the same ledger to the idempotent handler; then the provider
    sends evt_0001 again, and a new payment's webhook twice. The idempotent handler books evt_0001 a second time (it
    has no record of it) and the new event once."""
    provider = FakeProvider(SECRET, seed=1)
    provider.create_payment("cus_001", 2_500)  # evt_0001
    provider.create_payment("cus_002", 4_900)  # evt_0002
    ledger = Ledger()
    naive = WebhookEndpoint(ledger, SECRET, "naive", now=provider.clock.now)
    provider.deliver(naive, provider.plan("duplicate", duplicate_event_id="evt_0002"))

    def checked():
        found = check_money(provider, ledger.conn)
        return lambda wait, timeout_seconds: live.LiveCheck(provider.clock.now(), None, Records(provider.events), found)

    at_action = verify.read(checked(), 1.0, lambda: BOUNDARY)  # right after the rollback
    idempotent = WebhookEndpoint(ledger, SECRET, "idempotent", now=provider.clock.now)
    provider.clock.advance(5)
    provider.deliver(idempotent, [p for p in provider.plan("normal") if p.event.id == "evt_0001"])
    provider.create_payment("cus_003", 1_250)  # evt_0003, made after the action
    provider.deliver(idempotent, [p for p in provider.plan("normal") if p.event.id == "evt_0003"] * 2)
    after = verify.read(checked(), 1.0, lambda: BOUNDARY + 60)
    assert [f.key for f in after.findings if f.kind == verify.COUNTED_TWICE] == [
        "event evt_0001 posted 2 times",
        "event evt_0002 posted 2 times",
    ]
    money = verify.judge(at_action, after)
    assert money.cause_removed is True and money.right is False
    assert "event evt_0002 posted 2 times" in money.judged[BEFORE]
    assert "event evt_0001 posted 2 times" in money.judged[SURFACED]
    assert money.judged[NEW] == []


# Health


def evaluation_of(readings):
    return rules(readings).evaluate()


def test_the_metric_rules_are_quiet_when_each_read_its_signal_and_none_holds():
    assert verify.metrics_unwell(evaluation_of(Readings())) is None


def test_the_money_rule_is_left_out_of_health():
    failed = {"outcome": "failed", "invariants": 5, "failed": {"posted_exactly_once": 7}, "money_off_cents": 46_100}
    assert verify.metrics_unwell(evaluation_of(Readings(money=failed))) is None


@pytest.mark.parametrize(
    "series, why",
    [
        ({"p95_seconds": [({}, 9.4)]}, "a rule's condition holds: PaymentsLatencyHigh (payments-api)"),
        (
            {"lag_by_group": [({"consumer": "ledger"}, 271.0), ({"consumer": "payment-confirmer"}, 0.0)]},
            "ConsumerLagHigh (ledger)",
        ),
        ({"p95_seconds": []}, "no data for PaymentsLatencyHigh (payments-api)"),
        ({"lag_by_group": [({"consumer": "ledger"}, 0.0)]}, "no data for ConsumerLagHigh (payment-confirmer)"),
        (
            {"requests_per_second": httpx.ConnectError("x")},
            "no data for PaymentsLatencyHigh (payments-api), PaymentsErrorsHigh",
        ),
    ],
    ids=["slow", "behind", "no p95", "a group unread", "Prometheus unread"],
)
def test_the_metric_rules_are_not_quiet_when_one_holds_or_cannot_be_read(series, why):
    found = verify.metrics_unwell(evaluation_of(Readings(**series)))
    assert found is not None and why in found


def row(service, state="running", health="healthy", code=0):
    return {"Service": service, "State": state, "Health": health, "ExitCode": code}


HEALTHY_ROWS = [
    row("ledger-consumer"),
    row("payments-api"),
    row("kafka-init", "exited", "", 0),
    row("plain", health=""),
]


def test_every_container_healthy_and_the_one_shot_exited_0_is_healthy():
    assert verify.containers_unwell(HEALTHY_ROWS) is None


@pytest.mark.parametrize(
    "rows, why",
    [
        ([*HEALTHY_ROWS, row("payment-confirmer", health="unhealthy")], "payment-confirmer unhealthy"),
        ([*HEALTHY_ROWS, row("ledger-consumer", health="starting")], "ledger-consumer starting"),
        ([*HEALTHY_ROWS, row("redis", state="exited", health="")], "redis exited"),
        ([row("kafka-init", "exited", "", 1)], "kafka-init exited"),
        ([], "docker lists no container"),
    ],
)
def test_a_container_that_is_not_healthy_is_named(rows, why):
    assert why in verify.containers_unwell(rows)


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def rounds_of(*readings):
    """The rules' evaluations, one per round, from these readings in turn (the last one again after them)."""
    alerting = rules(Readings())
    queue = list(readings)

    def evaluate():
        alerting.prometheus = (queue.pop(0) if len(queue) > 1 else queue[0]).prometheus
        return alerting.evaluate()

    return evaluate


def test_healthy_means_quiet_at_two_rounds_in_a_row():
    clock = Clock()
    health = verify.wait_healthy(rounds_of(Readings()), lambda: HEALTHY_ROWS, clock.sleep, clock)
    assert health.healthy and len(health.rounds) == 2 and clock.slept == [verify.EVERY_SECONDS]


def test_a_round_that_is_not_quiet_starts_the_count_again():
    clock = Clock()
    slow = Readings(p95_seconds=[({}, 9.4)])
    health = verify.wait_healthy(rounds_of(Readings(), slow, Readings()), lambda: HEALTHY_ROWS, clock.sleep, clock)
    assert health.healthy and [r["unwell"] is None for r in health.rounds] == [True, False, True, True]


def test_not_quiet_within_the_time_allowed_is_not_healthy_and_says_why():
    clock = Clock()
    health = verify.wait_healthy(
        rounds_of(Readings(lag_by_group=[({"consumer": "ledger"}, 900.0), ({"consumer": "payment-confirmer"}, 0.0)])),
        lambda: HEALTHY_ROWS,
        clock.sleep,
        clock,
    )
    assert not health.healthy and "ConsumerLagHigh (ledger)" in health.why
    assert len(health.rounds) == 13 and clock.now == verify.HEALTHY_WITHIN_SECONDS  # rounds at 0, 15, ..., 180 s


def test_containers_that_are_not_healthy_or_docker_that_cannot_be_read_are_not_healthy():
    clock = Clock()

    def unreachable():
        raise OSError("docker is not running")

    health = verify.wait_healthy(rounds_of(Readings()), unreachable, clock.sleep, clock, within=30.0)
    assert not health.healthy and health.why == "docker could not be read (OSError)"
    sick = [*HEALTHY_ROWS, row("ledger-consumer", health="unhealthy")]
    health = verify.wait_healthy(rounds_of(Readings()), lambda: sick, clock.sleep, clock, within=30.0)
    assert not health.healthy and "ledger-consumer unhealthy" in health.why


# The verdict


class Checks:
    """Made-up money checks, one per call, in turn; each call noted with the time it was made."""

    def __init__(self, clock, *made):
        self.clock, self.made, self.at = clock, list(made), []

    def __call__(self, wait, timeout_seconds):
        self.at.append((self.clock(), wait, timeout_seconds))
        return self.made.pop(0)(wait, timeout_seconds)


def verified(*made, readings=None, containers=None, observe=verify.OBSERVE_SECONDS):
    clock = Clock()
    checks = Checks(clock, *made)
    found = verify.verify(
        rounds_of(readings or Readings()),
        checks,
        containers or (lambda: HEALTHY_ROWS),
        clock.sleep,
        clock,
        lambda: BOUNDARY + clock.now,
        observe=observe,
    )
    return found, checks


def test_a_rollback_that_removed_the_cause_but_left_old_damage_passes_and_says_both():
    old = event(1, EARLY)
    failed = {"posted_exactly_once": [posted(old)], "matches_provider": [differs("pay_0001", 2_500), total(2_500)]}
    found, checks = verified(check(BOUNDARY, Records([old]), **failed), check(LATE, Records([old]), **failed))
    assert found.verdict == "passed"
    assert found.said == (
        "Healthy: every container is healthy, and the metric rules were quiet at 2 evaluations in a row.",
        "The cause is removed: nothing the provider made after the action was booked wrong.",
        "The money is not right: 1 event counted more than once, 1 payment whose books differ from the provider's;"
        " our books $25.00 ahead of the provider's balance. A rollback repairs nothing: this damage stays until a"
        " person repairs it.",
    )
    # the check right after the action, then the later one, no sooner than a minute after it; both wait to settle
    assert [(at, wait) for at, wait, _ in checks.at] == [(0.0, True), (verify.OBSERVE_SECONDS, True)]
    assert {timeout for _, _, timeout in checks.at} == {verify.SETTLE_SECONDS}


def test_a_rollback_that_fixed_everything_passes_with_the_money_right():
    found, _ = verified(check(BOUNDARY), check(LATE))
    assert found.verdict == "passed" and found.said[1:] == (
        "The cause is removed: nothing the provider made after the action was booked wrong.",
        "The money is right: the money check passed.",
    )


def test_new_damage_after_the_action_fails_the_verification():
    late = event(2, LATE)
    found, _ = verified(check(BOUNDARY), check(LATE, Records([late]), posted_exactly_once=[posted(late)]))
    assert found.verdict == "failed"
    assert found.said[1] == (
        "The cause is not removed: 1 finding is new since the action, or cannot be dated: event evt_0002 posted 2"
        " times."
    )


def test_a_stack_that_is_not_healthy_in_time_fails_the_verification_whatever_the_money():
    found, _ = verified(check(BOUNDARY), check(LATE), readings=Readings(p95_seconds=[({}, 9.4)]))
    assert found.verdict == "failed" and found.said[0].startswith("Not healthy within 180 s: a rule's condition holds")
    assert found.money.right is True  # said apart: the money can be right while the stack is not healthy


def test_a_money_check_that_cannot_run_after_the_action_cannot_verify():
    def broken(wait, timeout_seconds):
        raise OSError("x")

    found, _ = verified(check(BOUNDARY), broken)
    assert found.verdict == "could not verify"
    assert found.said[1:] == (
        "Whether the cause is removed is not known: the money check after the action could not run: the money check"
        " could not run (OSError).",
        "Whether the money is right is not known: the money check could not run (the money check could not run"
        " (OSError)).",
    )


def test_damage_seen_later_is_said_as_from_before_the_action():
    old, again = event(1, EARLY), event(2, EARLY)
    found, _ = verified(
        check(BOUNDARY, Records([old, again]), posted_exactly_once=[posted(old)]),
        check(LATE, Records([old, again]), posted_exactly_once=[posted(old), posted(again)]),
    )
    assert found.verdict == "passed"
    assert (
        "1 finding showed only after the action, about records made before it: damage from before the action, seen"
        " later."
    ) in found.said


def test_a_slow_health_check_is_not_waited_for_twice():
    # the later check comes a minute after the action, or as soon as health is known, whichever is later
    found, checks = verified(check(BOUNDARY), check(LATE), observe=10.0)
    assert [at for at, _, _ in checks.at] == [0.0, verify.EVERY_SECONDS]


def test_the_verification_saved_in_the_evidence_holds_what_it_said_and_read():
    found, _ = verified(check(BOUNDARY), check(LATE))
    saved = found.to_dict()
    assert saved["verdict"] == "passed" and saved["said"] == list(found.said)
    assert Reading.from_dict(saved["at_action"]) == found.at_action and saved["health"]["healthy"] is True


def test_health_and_money_use_the_same_rules_as_klaxons_alerting():
    # One source of truth: the thresholds and groups verification reads are klaxon.alerts' own.
    assert verify.EVERY_SECONDS == alerts.EVERY_SECONDS
    assert {subject for _, subject, _ in verify.METRICS} == {"payments-api"}
    assert HEALTHY["lag_by_group"] and PASSED["outcome"] == "passed"
