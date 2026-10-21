"""Verification after an approved action: is the system healthy again, and is the money right?

An action that ran (klaxon.agent.gate) does not end an incident. After it, code checks two things, and never the
model:

    health  every container is healthy (the one-shot kafka-init has exited 0), and the metric rules of klaxon.alerts
            are quiet at 2 evaluations in a row: every signal read, no condition holding. Within 3 minutes of the
            first evaluation, which comes once the first money check below is done.
    money   the money check (klaxon.checks.live) right after the action, and again at least 60 s after it.

The money rule is left out of health: the money is judged on its own, below, because a rollback can remove a cause
and still leave the money wrong. So the money has two questions, said apart:

    the cause is removed   no new damage after the action: nothing the provider made in the minute after it was
                           booked wrong
    the money is right     the money check passes

A rollback stops new damage and repairs nothing. Money booked wrong before it stays wrong until a person repairs it,
and verification says what damage remains.

How each finding of the later money check is judged:
    1. The check right after the action had the same finding, about records made up to its cutoff (or that cannot
       be dated): damage from before the action. That check also judges some findings whatever the cutoff (a
       payment charged twice, a charge refunded more than its amount), so it can see one about a record made after
       its cutoff, after the action: that one goes on to 3, and is new.
    2. Else, if everything it is about (the provider's events, charges or refunds) was made up to that check's
       cutoff: damage from before the action that the check right after it did not show. Mostly because that check
       could not settle (a backlog still draining), so it showed nothing. Rarely otherwise: that check waits until
       every webhook up to its cutoff is acknowledged and handled, and the provider never sends an acknowledged one
       again, so only a webhook that stalled in the network and landed late (the gap klaxon.checks.live names) can
       add an old event's posting after it.
    3. Else, made after it, or impossible to date: new damage. Klaxon says the cause is removed only when it can show
       it.
A payment whose books differ from the provider's, when the first check had no such finding, follows the findings
about its own events: from before the action, found later, when one of them is that and none is new; else new, a
change nothing explains. The difference over all payments is their sum, and follows all the other findings the same
way.

Why the dates matter: a rollback does not dedupe the past. The naive ledger handler keeps no record of the events it
books (in the live ledger, klaxon.patient.services.pg_ledger, only the idempotent mode writes processed_events). So
after a rollback to the idempotent handler, a webhook the provider sends again for such an event, its first
acknowledgement lost, is booked once more. The check right after the action waits for those webhooks and counts
what they booked, so it is damage at the action.

The verdict: passed (healthy, and the cause removed), failed (not healthy, or new damage), or could not verify (no
money check that could run, so whether the cause is removed is not known). A failed verification makes Klaxon
propose undoing its action, through the gate (klaxon.agent.gate.propose_undo).

Its limit: it sees what a minute of traffic shows. A cause that shows only now and then may not show in that minute.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass

from klaxon import alerts
from klaxon.agent.tools import ONE_SHOT, clean_text
from klaxon.checks import live
from klaxon.checks.money import OVER_REFUNDED, MoneyReport, format_cents
from klaxon.patient import stack
from klaxon.patient.deploys import utc_now

EVERY_SECONDS = alerts.EVERY_SECONDS  # between two evaluations of the rules
QUIET_IN_A_ROW = 2  # evaluations in a row at which the metric rules must be quiet and every container healthy
HEALTHY_WITHIN_SECONDS = 180.0  # from health's first evaluation: not healthy by then, and the verification fails
OBSERVE_SECONDS = 60.0  # the later money check comes no sooner than this after the action
SETTLE_SECONDS = 60.0  # how long each money check may wait to settle (a drained backlog takes a while)
DOCKER_TIMEOUT = 30.0
METRICS = (("PaymentsLatencyHigh", "payments-api", "p95_seconds"), ("PaymentsErrorsHigh", "payments-api", "share"))
TOTAL = "provider_clearing, all payments"  # matches_provider's finding over every payment: their sum
MAX_KEY = 300

# What a finding is, by kind: the kinds the counts use.
COUNTED_TWICE, LOST, NEVER_SENT = "counted more than once", "lost", "never sent"
UNBALANCED, BOOKS_DIFFER, TOTAL_DIFFERS = "unbalanced", "books differ", "total differs"
PAYMENT, REFUND, OTHER = "payment", "refund", "other"
EVENTS = (COUNTED_TWICE, LOST)  # the findings about one event the provider sent, so about its payment
# How a finding of the later check is judged.
BEFORE, SURFACED, NEW = "before the action", "from before the action, found only by the later check", "new"


@dataclass(frozen=True)
class Finding:
    """One thing the money check found wrong, as verification keeps it."""

    invariant: str
    kind: str
    key: str  # what it is about, and what is wrong with it: the same damage has the same key in every check
    created: int | None = None  # when the newest record it is about was made, by the provider's clock; None: unknown
    payment: str | None = None  # the payment it is about, when there is one


@dataclass(frozen=True)
class Reading:
    """One money check, as verification keeps it: its outcome, its cutoff, and what it found."""

    at_utc: str
    outcome: str  # passed, failed, could not settle, could not run
    cutoff: int | None  # the provider's clock, unix seconds: what the provider made up to the end of it was judged
    findings: tuple[Finding, ...] = ()
    money_off_cents: int | None = None  # our ledger minus the provider's balance
    behind: dict[str, int] | None = None  # could not settle: messages up to the cutoff not handled yet, per group
    why: str | None = None  # could not settle or run: why, in this module's words

    def counts(self) -> dict[str, int]:
        return dict(Counter(finding.kind for finding in self.findings))

    def to_dict(self) -> dict:
        return asdict(self) | {"counts": self.counts()}

    @classmethod
    def from_dict(cls, value: Mapping) -> Reading:
        """A reading saved in a run's evidence, read back: so a verdict can be worked out again, offline."""
        kept = {key: value[key] for key in cls.__dataclass_fields__ if key != "findings"}
        return cls(**kept, findings=tuple(Finding(**item) for item in value["findings"]))


def read(
    check: Callable[..., live.LiveCheck] = live.check_live,
    timeout: float = SETTLE_SECONDS,
    now: Callable[[], float] = time.time,
) -> Reading:
    """One money check, waiting up to timeout seconds to settle. Never raises: a check that cannot settle or run is
    a reading too, with why."""
    at = utc_now(now)
    try:
        result = check(wait=True, timeout_seconds=timeout)
    except live.NotSettled as exc:
        settling = exc.settling
        behind = None
        if isinstance(settling.behind, dict):
            behind = {clean_text(str(group), 40): n for group, n in settling.behind.items() if isinstance(n, int)}
        return Reading(at, "could not settle", settling.cutoff, behind=behind, why=_not_settled(settling, behind))
    except live.ClockProblem:
        return Reading(at, "could not run", None, why="the provider's clock and its records disagree")
    except Exception as exc:  # out of reach, or an answer the check refuses to read: no verdict either way
        return Reading(at, "could not run", None, why=f"the money check could not run ({type(exc).__name__})")
    report = result.report
    return Reading(
        at,
        "passed" if report.passed else "failed",
        result.cutoff,
        findings_of(report, result.provider),
        int(report.money_off_cents),
    )


def _not_settled(settling: live.Settling, behind: Mapping[str, int] | None) -> str:
    waited = settling.waited_seconds if isinstance(settling.waited_seconds, int | float) else "?"
    if behind is None:
        return f"not settled after {waited} s: webhooks up to its cutoff not acknowledged by our receiver yet"
    groups = ", ".join(f"{group} {n}" for group, n in behind.items())
    return f"not settled after {waited} s: messages up to its cutoff not handled yet: {groups}"


def findings_of(report: MoneyReport, provider: object) -> tuple[Finding, ...]:
    """Every finding of a failed check, each with the time of the newest provider record it is about, when known."""
    events = {event.id: event for event in _records(provider, "events")}
    charges = {charge.id: charge.created for charge in _records(provider, "payments")}
    refunds = {refund.id: refund.created for refund in _records(provider, "refunds")}
    out = []
    for invariant in report.invariants:
        if invariant.passed:
            continue
        for item in invariant.evidence:
            out.append(_finding(invariant.name, item, events, charges, refunds))
    return tuple(out)


def _records(provider: object, name: str) -> list:
    """The provider's records of one kind, as klaxon.checks.live reads them: each with an id and a created stamp.
    Anything else is left out, so a record that cannot be dated never dates a finding."""
    found = getattr(provider, name, None)
    if not isinstance(found, list | tuple):
        return []
    return [item for item in found if isinstance(getattr(item, "id", None), str) and hasattr(item, "created")]


def _key(text: str) -> str:
    return clean_text(text, MAX_KEY)


def _id(value: object) -> str:
    return clean_text(str(value), 64)


def _newest(ids: Sequence[object], made: Mapping[str, int | None]) -> int | None:
    """The newest of these records' times; None when there is none, or one of them has no time."""
    stamps = [made.get(str(record)) for record in ids]
    known = [stamp for stamp in stamps if stamp is not None]
    return max(known) if known and len(known) == len(stamps) else None


def _finding(name: str, item: Mapping, events: Mapping, charges: Mapping, refunds: Mapping) -> Finding:
    if name == "posted_exactly_once":
        event_id, n = str(item.get("event_id")), item.get("times_posted")
        if "type" not in item:  # a posting of an event the provider never sent: nothing to date it by
            return Finding(name, NEVER_SENT, _key(f"event {event_id} posted {n} times, never sent"))
        event = events.get(event_id)
        return Finding(
            name,
            LOST if n == 0 else COUNTED_TWICE,
            _key(f"event {event_id} posted {n} times"),
            None if event is None else event.created,
            _id(item.get("payment_id")),
        )
    if name == "ledger_balanced":
        if "scope" in item:  # by how far apart they are: the totals themselves grow with every booking
            debits, credits = item.get("debits_cents"), item.get("credits_cents")
            off = debits - credits if isinstance(debits, int) and isinstance(credits, int) else "?"
            text = f"the whole ledger: debits minus credits {off} cents"
        else:
            text = f"transaction {item.get('transaction_id')} has {item.get('lines')} lines, unbalanced"
        return Finding(name, UNBALANCED, _key(text))
    if name == "matches_provider":
        scope, off = str(item.get("scope")), item.get("difference_cents")
        if scope == TOTAL:
            return Finding(name, TOTAL_DIFFERS, _key(f"all payments: books off by {off} cents"))
        return Finding(name, BOOKS_DIFFER, _key(f"payment {scope}: books off by {off} cents"), payment=_id(scope))
    if name == "payments_match_provider":
        found = [charge.get("id") for charge in item.get("provider_charges") or []]
        text = f"payment {item.get('payment_id')} ({', '.join(map(str, found)) or 'no charge'}): {item.get('problem')}"
        return Finding(name, PAYMENT, _key(text), _newest(found, charges))
    if name == "refunds_match_provider":
        found = [refund.get("id") for refund in item.get("provider_refunds") or []]
        if item.get("problem") == OVER_REFUNDED:  # about a charge and all its refunds, not one refund of ours
            text = (
                f"charge {item.get('provider_payment_id')} ({', '.join(map(str, found))}): refunded"
                f" {item.get('refunded_cents')} cents of {item.get('charged_cents')}"
            )
            return Finding(name, REFUND, _key(text), _newest(found, refunds))
        text = f"refund {item.get('refund_id')} ({', '.join(map(str, found)) or 'no refund'}): {item.get('problem')}"
        return Finding(name, REFUND, _key(text), _newest(found, refunds))
    return Finding(name, OTHER, _key(f"{name}: {json.dumps(item, sort_keys=True, default=str)}"))


# The money: what is new since the action.


@dataclass(frozen=True)
class Money:
    """The money after the action: whether the cause is removed, whether the money is right, and each later finding
    as judged. None: not known."""

    cause_removed: bool | None
    right: bool | None
    judged: dict[str, list[str]]  # BEFORE, SURFACED, NEW: the keys of the later check's findings
    why: str | None = None  # when the cause is not known: why


def judge(at_action: Reading, after: Reading) -> Money:
    """The later check's findings, against the check right after the action (module docstring)."""
    judged: dict[str, list[str]] = {BEFORE: [], SURFACED: [], NEW: []}
    if after.outcome == "passed":
        return Money(True, True, judged)
    if after.outcome != "failed":
        return Money(None, None, judged, f"the money check after the action {after.outcome}: {after.why}")
    if at_action.cutoff is None:  # no check right after the action could even begin: nothing to compare with
        why = f"the money check right after the action {at_action.outcome}: {at_action.why}"
        return Money(None, False, judged, why)
    known = {finding.key for finding in at_action.findings}
    boundary = at_action.cutoff
    findings = after.findings
    verdicts: list[str | None] = [
        BEFORE if finding.key in known and (finding.created is None or finding.created <= boundary) else None
        for finding in findings
    ]
    for n, finding in enumerate(findings):  # first what can be dated by itself
        if verdicts[n] is None and finding.kind not in (BOOKS_DIFFER, TOTAL_DIFFERS):
            verdicts[n] = SURFACED if finding.created is not None and finding.created <= boundary else NEW
    for n, finding in enumerate(findings):  # then a payment's books, as the findings about its events
        if verdicts[n] is None and finding.kind == BOOKS_DIFFER:
            own = [verdicts[m] for m, f in enumerate(findings) if f.payment == finding.payment and f.kind in EVENTS]
            verdicts[n] = _follows(own)
    others = [verdict for m, verdict in enumerate(verdicts) if findings[m].kind != TOTAL_DIFFERS]
    for n in range(len(findings)):  # last, the total, as everything else
        if verdicts[n] is None:
            verdicts[n] = _follows(others)
    for finding, verdict in zip(findings, verdicts, strict=True):
        judged[str(verdict)].append(finding.key)
    return Money(not judged[NEW], False, judged)


def _follows(verdicts: Sequence[str | None]) -> str:
    """The verdict of a finding that follows others (a payment's books, the total): from before the action, found
    later, when one of them is that and none is new; else new, since nothing explains the change. One that is the
    same as in the first check never gets here."""
    return SURFACED if SURFACED in verdicts and NEW not in verdicts else NEW


# Health: the containers, and the metric rules.


def read_containers() -> list[dict]:
    """Every container of the stack, once docker is known to be this Mac's. Reads only."""
    stack.ensure_local(timeout=DOCKER_TIMEOUT)
    return stack.containers(timeout=DOCKER_TIMEOUT)


def metrics_unwell(evaluation: alerts.Evaluation) -> str | None:
    """Why the metric rules are not quiet at this evaluation, or None: each must have read its signal, with no
    condition holding. The money rule is left out: the money is judged apart."""
    read = {(check.rule, check.subject): check for check in evaluation.checks}
    missing, held = [], []
    wanted = [*METRICS, *(("ConsumerLagHigh", group, "messages_behind") for group in alerts.CONSUMER_GROUPS.values())]
    for rule, subject, number in wanted:
        check = read.get((rule, subject))
        if check is None or check.numbers.get(number) is None:
            missing.append(f"{rule} ({subject})")
        elif check.breached:
            held.append(f"{rule} ({subject})")
    if held:
        return f"a rule's condition holds: {', '.join(held)}"
    if missing:
        return f"no data for {', '.join(missing)}"
    return None


def containers_unwell(rows: Sequence[Mapping]) -> str | None:
    """Why the containers are not all healthy, or None. A container with a health check must be healthy, one with
    none must be running, and the one-shot kafka-init must have exited 0."""
    if not rows:
        return "docker lists no container of the stack"
    wrong = []
    for row in rows:
        service = clean_text(str(row.get("Service")), 64)
        state, health = row.get("State"), row.get("Health") or ""
        if service in ONE_SHOT:
            fine = state == "exited" and row.get("ExitCode") == 0
        else:
            fine = state == "running" and health in ("", "healthy")
        if not fine:
            wrong.append(f"{service} {clean_text(str(health or state), 20)}")
    return f"not healthy: {', '.join(sorted(wrong))}" if wrong else None


@dataclass(frozen=True)
class Health:
    healthy: bool
    why: str | None  # the last reason it was not, when it was not healthy in time
    rounds: tuple[dict, ...]  # each round: when, and why it was not quiet (None: quiet)


def wait_healthy(
    rules: Callable[[], alerts.Evaluation],
    containers: Callable[[], list[dict]] = read_containers,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    within: float = HEALTHY_WITHIN_SECONDS,
    every: float = EVERY_SECONDS,
) -> Health:
    """Evaluate the rules, and read the containers, every `every` seconds, until both are fine at QUIET_IN_A_ROW
    rounds in a row; at most `within` seconds."""
    started = clock()
    rounds: list[dict] = []
    in_a_row = 0
    while True:
        evaluation = rules()
        why = metrics_unwell(evaluation)
        try:
            unwell = containers_unwell(containers())
        except Exception as exc:  # docker out of reach: not healthy, as far as anyone can tell
            unwell = f"docker could not be read ({type(exc).__name__})"
        why = "; ".join(part for part in (why, unwell) if part) or None
        rounds.append({"at_utc": evaluation.at_utc, "unwell": why})
        in_a_row = 0 if why else in_a_row + 1
        if in_a_row >= QUIET_IN_A_ROW:
            return Health(True, None, tuple(rounds))
        if clock() - started + every > within:
            return Health(False, why or "not quiet long enough", tuple(rounds))
        sleep(every)


# The verdict.


@dataclass(frozen=True)
class Verification:
    """What code found after the action: numbers, and the same in plain sentences (said)."""

    verdict: str  # passed, failed, could not verify
    health: Health
    at_action: Reading  # the money check right after the action
    after: Reading  # the money check at least a minute after it
    money: Money

    @property
    def said(self) -> tuple[str, ...]:
        return tuple(say(self))

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "said": list(self.said),
            "health": asdict(self.health),
            "money": asdict(self.money),
            "at_action": self.at_action.to_dict(),
            "after": self.after.to_dict(),
        }


def verify(
    rules: Callable[[], alerts.Evaluation],
    check: Callable[..., live.LiveCheck] = live.check_live,
    containers: Callable[[], list[dict]] = read_containers,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
    observe: float = OBSERVE_SECONDS,
    within: float = HEALTHY_WITHIN_SECONDS,
    every: float = EVERY_SECONDS,
) -> Verification:
    """Verify an action that has just run: the money check right after it, health, then the money check again, no
    sooner than `observe` seconds after the action. rules: one evaluation of the symptom rules, with fresh counts."""
    acted = clock()
    at_action = read(check, SETTLE_SECONDS, now)
    health = wait_healthy(rules, containers, sleep, clock, within, every)
    left = observe - (clock() - acted)
    if left > 0:
        sleep(left)
    after = read(check, SETTLE_SECONDS, now)
    money = judge(at_action, after)
    if not health.healthy or money.cause_removed is False:
        verdict = "failed"
    elif money.cause_removed is None:
        verdict = "could not verify"
    else:
        verdict = "passed"
    return Verification(verdict, health, at_action, after, money)


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# How damage() says each kind, one and many. The total over all payments is said as the money off, not counted.
SAID = {
    COUNTED_TWICE: ("event counted more than once", "events counted more than once"),
    LOST: ("event lost", "events lost"),
    NEVER_SENT: ("posting of an event the provider never sent", "postings of events the provider never sent"),
    UNBALANCED: ("unbalanced transaction", "unbalanced transactions"),
    BOOKS_DIFFER: ("payment whose books differ from the provider's", "payments whose books differ from the provider's"),
    PAYMENT: (
        "payment that disagrees with the provider's charges",
        "payments that disagree with the provider's charges",
    ),
    REFUND: ("refund that disagrees with the provider's refunds", "refunds that disagree with the provider's refunds"),
    OTHER: ("other finding", "other findings"),
}


def damage(reading: Reading) -> str:
    """What a reading found, in one line: the counts, and how far our books are from the provider's."""
    if reading.outcome == "passed":
        return "no damage: the money check passed"
    if reading.outcome != "failed":
        return f"not known: the money check {reading.outcome} ({reading.why})"
    counts = reading.counts()
    parts = [_n(counts[kind], *SAID[kind]) for kind in SAID if counts.get(kind)]
    text = ", ".join(parts) or "a difference over all payments only"
    off = reading.money_off_cents
    if off:
        text += f"; our books {format_cents(abs(off))} {'ahead of' if off > 0 else 'behind'} the provider's balance"
    return text


def say(found: Verification) -> list[str]:
    """The verification in plain sentences: health, then whether the cause is removed, then whether the money is
    right, said apart."""
    health, money = found.health, found.money
    if health.healthy:
        lines = [
            "Healthy: every container is healthy, and the metric rules were quiet at"
            f" {QUIET_IN_A_ROW} evaluations in a row."
        ]
    else:
        lines = [f"Not healthy within {HEALTHY_WITHIN_SECONDS:g} s: {health.why}."]
    if money.cause_removed is True:
        lines.append(
            "The cause is removed: nothing the provider made between the action and the last money check was booked"
            " wrong."
        )
    elif money.cause_removed is False:
        new = money.judged[NEW]
        lines.append(
            f"The cause is not removed: {_n(len(new), 'finding is', 'findings are')} new since the action, or cannot"
            f" be dated: {'; '.join(new[:3])}{'; ...' if len(new) > 3 else ''}."
        )
    else:
        lines.append(f"Whether the cause is removed is not known: {money.why}.")
    surfaced = len(money.judged[SURFACED])
    if surfaced and found.at_action.outcome in ("passed", "failed"):
        lines.append(
            f"{_n(surfaced, 'finding', 'findings')} showed only after the money check right after the action, about"
            " records made before it: damage from before the action, seen later."
        )
    elif surfaced:
        lines.append(
            f"{_n(surfaced, 'finding is', 'findings are')} about records made before the action, which the money check"
            f" right after it could not see (it {found.at_action.outcome}): damage from before the action."
        )
    after = found.after
    if money.right is True:
        lines.append("The money is right: the money check passed.")
    elif money.right is False and after.outcome == "failed":
        lines.append(
            f"The money is not right: {damage(after)}. A rollback repairs nothing: this damage stays until a person"
            " repairs it."
        )
    else:
        lines.append(f"Whether the money is right is not known: the money check {after.outcome} ({after.why}).")
    return lines
