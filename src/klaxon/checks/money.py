"""The money check: do our books agree with what really happened at the provider?

Klaxon will run this after every fix, next to the health checks, because a
system can look healthy while the money is wrong.

Five invariants, each with evidence:
1. posted_exactly_once: every provider money event is in the ledger exactly
   once (0 times = lost, 2 or more = counted twice), and the ledger has nothing
   the provider never sent. A posting cancelled by a reversal does not count.
2. ledger_balanced: total debits equal total credits, and every transaction
   has at least two lines and balances on its own.
3. matches_provider: the ledger's provider_clearing balance equals what the
   provider says it holds, overall and per payment.
4. payments_match_provider: our payments table agrees with the provider's
   payments. Each of our payments the provider charged is succeeded, for the
   same amount, linked to that charge, and charged exactly once; one we call
   succeeded was charged; and the provider charged nothing that is not ours.
   A pending or failed payment with no charge is fine: no money moved.
5. refunds_match_provider: our refunds table agrees with the provider's
   refunds, the same way (week 2). Each refund of ours the provider made is
   succeeded, for the same amount and the same payment, linked to that
   refund, and made exactly once; one we call succeeded was made; and the
   provider refunded nothing that is not ours. A pending or failed refund
   the provider never made is fine: no money moved back.

Invariant 2 alone is not enough: a double posting is itself balanced. And
invariants 1-3 compare the ledger with the provider, so they pass whenever
the ledger copies the provider faithfully, even when the provider charged a
customer twice for one payment, or charged the wrong amount. Only invariants
4 and 5 compare the provider with what we asked for.

With a cutoff (week 2, for a check while traffic flows; see klaxon.checks.live):
only what the provider did up to the end of the cutoff's second is judged.
A provider record stamped later, and anything our side made from it, may
still be on its way, so it is left for the next check. Everything up to the
cutoff is judged exactly as without one. Four things are judged whatever the
cutoff, because no timing explains them: a posting the provider never sent,
a payment charged twice (or a refund made twice), a payment we call
succeeded with no charge at all (or a refund with no refund), and one linked
to a charge (or a refund) the provider gives to another of ours.
(The live check reads our books before the provider's records, so whatever
our books hold from the provider is already in those records.)

The check reads the ledger and our payments with its own SELECT queries, not
through the Ledger class, the handlers or the payments API, so it does not
depend on their bookkeeping being right. Invariants 1-3 run on the first
piece's SQLite ledger and on the live PostgreSQL ledger (week 1), where the
check connects through a read-only role (see `klaxon.checks.live`). Sums are
cast to BIGINT so both return plain ints. Invariants 4 and 5 need our
payments and refunds tables and the provider's payment and refund records,
which only the live stack has (week 2).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from klaxon.patient.provider import PAYMENT_SUCCEEDED, REFUND_SUCCEEDED, Event

# How each provider event moves the money the provider holds for us. Other
# event types move no money: the ledger does not book them and this check
# ignores them.
MONEY_EFFECT = {PAYMENT_SUCCEEDED: +1, REFUND_SUCCEEDED: -1}


class ProviderRecords(Protocol):
    events: list[Event]

    def balance_cents(self) -> int: ...


@dataclass(frozen=True)
class ProviderPayment:
    """One payment as the provider records it. Every payment at this provider is captured at once.

    klaxon_payment_id is our own payment id, which the payments API sends with every charge as
    metadata; None when the metadata holds none. created: when it was charged, by the provider's clock
    (unix seconds); None when not known, and then a cutoff never leaves it out.
    """

    id: str
    amount_cents: int
    klaxon_payment_id: str | None
    created: int | None = None


@dataclass(frozen=True)
class ProviderRefund:
    """One refund as the provider records it. Every refund at this provider succeeds at once, or not at all.

    payment_id is the provider's id for the payment it refunded. klaxon_refund_id is our own refund id, which
    the payments API sends with every refund as metadata; None when the metadata holds none. created: as for
    a payment.
    """

    id: str
    payment_id: str | None
    amount_cents: int
    klaxon_refund_id: str | None
    created: int | None = None


class SqlConnection(Protocol):
    """What the check needs from a database connection. sqlite3 and psycopg connections both fit."""

    def execute(self, query: str) -> Any: ...


@dataclass
class InvariantResult:
    name: str
    passed: bool
    summary: str
    evidence: list[dict] = field(default_factory=list)


@dataclass
class MoneyReport:
    passed: bool
    ledger_clearing_cents: int
    provider_balance_cents: int
    money_off_cents: int  # ledger minus provider; positive = our books claim money that is not there
    invariants: list[InvariantResult]
    cutoff: int | None = None  # the provider's clock, unix seconds; None: everything was judged
    left_for_next_check: dict[str, int] = field(default_factory=dict)  # newer than the cutoff, not judged yet

    def invariant(self, name: str) -> InvariantResult:
        return next(i for i in self.invariants if i.name == name)

    def to_dict(self) -> dict:
        return asdict(self)


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def format_cents(cents: int, signed: bool = False) -> str:
    sign = "-" if cents < 0 else ("+" if signed and cents > 0 else "")
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def check_money(
    provider: ProviderRecords,
    ledger: SqlConnection,
    provider_payments: list[ProviderPayment] | None = None,
    cutoff: int | None = None,
    provider_refunds: list[ProviderRefund] | None = None,
) -> MoneyReport:
    """Invariants 1-3; invariant 4 when the provider's payments are given, and invariant 5 when its refunds are
    (ledger must then reach our payments and refunds tables too, as the live stack's read-only role does).

    cutoff: judge only what the provider did up to the end of that second (its clock, unix seconds). Then
    provider.balance_cents() must be its balance as of the cutoff, and provider.events must hold its newer
    events too: they tell a posting made after the cutoff from one the provider never sent.
    """
    money_events = [e for e in provider.events if e.type in MONEY_EFFECT]
    events = [e for e in money_events if cutoff is None or e.created <= cutoff]
    newer = {e.id for e in money_events if cutoff is not None and e.created > cutoff}
    provider_balance = provider.balance_cents()
    # The ledger's provider_clearing money per (payment, event), without the postings of newer events.
    clearing = {key: net for key, net in _clearing_by_event(ledger).items() if key[1] not in newer}
    ledger_clearing = sum(clearing.values())
    invariants = [
        _posted_exactly_once(events, ledger, newer),
        _ledger_balanced(ledger),
        _matches_provider(events, provider_balance, ledger_clearing, clearing),
    ]
    left = {"provider_events": len(newer), "provider_charges": 0} if cutoff is not None else {}
    if provider_payments is not None:
        fourth, newer_charges = _payments_match_provider(provider_payments, ledger, cutoff)
        invariants.append(fourth)
        if cutoff is not None:
            left["provider_charges"] = newer_charges
    if provider_refunds is not None:
        fifth, newer_refunds = _refunds_match_provider(provider_refunds, ledger, cutoff)
        invariants.append(fifth)
        if cutoff is not None:
            left["provider_refunds"] = newer_refunds
    return MoneyReport(
        passed=all(i.passed for i in invariants),
        ledger_clearing_cents=ledger_clearing,
        provider_balance_cents=provider_balance,
        money_off_cents=ledger_clearing - provider_balance,
        invariants=invariants,
        cutoff=cutoff,
        left_for_next_check=left,
    )


def _live_postings(ledger: SqlConnection) -> dict[str, list[int]]:
    """Transaction ids per event that still count: they have lines, are not
    reversals, and have not been reversed."""
    rows = ledger.execute(
        "SELECT t.event_id, t.id FROM transactions t"
        " WHERE t.reverses_transaction_id IS NULL"
        "   AND NOT EXISTS (SELECT 1 FROM transactions r WHERE r.reverses_transaction_id = t.id)"
        "   AND EXISTS (SELECT 1 FROM entries e WHERE e.transaction_id = t.id)"
        " ORDER BY t.id"
    ).fetchall()
    out: dict[str, list[int]] = defaultdict(list)
    for event_id, tx_id in rows:
        out[event_id].append(tx_id)
    return out


def _posted_exactly_once(events: list[Event], ledger: SqlConnection, newer: set[str]) -> InvariantResult:
    postings = _live_postings(ledger)
    for event_id in newer:  # booked from an event newer than the cutoff: the next check judges it
        postings.pop(event_id, None)
    evidence = []
    for e in events:
        tx_ids = postings.pop(e.id, [])
        if len(tx_ids) != 1:
            n = len(tx_ids)
            problem = "lost" if n == 0 else "counted twice" if n == 2 else f"counted {n} times"
            evidence.append(
                {
                    "event_id": e.id,
                    "type": e.type,
                    "payment_id": e.payment_id,
                    "amount_cents": e.amount_cents,
                    "times_posted": n,
                    "expected": 1,
                    "problem": problem,
                    "transaction_ids": tx_ids,
                }
            )
    for event_id, tx_ids in sorted(postings.items()):  # what is left was never sent
        evidence.append(
            {
                "event_id": event_id,
                "times_posted": len(tx_ids),
                "expected": 0,
                "problem": "posted, but the provider never sent it",
                "transaction_ids": tx_ids,
            }
        )

    if not evidence:
        if not newer:
            return InvariantResult(
                "posted_exactly_once", True, f"all {len(events)} provider events posted exactly once"
            )
        summary = (
            f"all {len(events)} provider events up to the cutoff posted exactly once"
            f" ({_n(len(newer), 'newer one is', 'newer ones are')} left for the next check)"
        )
        return InvariantResult("posted_exactly_once", True, summary)
    parts = []
    for item in evidence:
        if "type" in item:
            parts.append(
                f"{item['event_id']} ({item['type']}, {item['payment_id']}, {format_cents(item['amount_cents'])})"
                f" posted {item['times_posted']} times, expected 1"
            )
        else:
            parts.append(f"{item['event_id']} posted {item['times_posted']} times, but the provider never sent it")
    return InvariantResult("posted_exactly_once", False, "; ".join(parts), evidence)


def _ledger_balanced(ledger: SqlConnection) -> InvariantResult:
    debits, credits = ledger.execute(
        "SELECT CAST(COALESCE(SUM(CASE side WHEN 'debit' THEN amount_cents END), 0) AS BIGINT),"
        "       CAST(COALESCE(SUM(CASE side WHEN 'credit' THEN amount_cents END), 0) AS BIGINT) FROM entries"
    ).fetchone()
    # PostgreSQL does not allow column aliases in HAVING, so filter in an outer query.
    bad = ledger.execute(
        "SELECT id, n, d, c FROM ("
        "  SELECT t.id AS id, COUNT(e.id) AS n,"
        "         CAST(COALESCE(SUM(CASE e.side WHEN 'debit' THEN e.amount_cents END), 0) AS BIGINT) AS d,"
        "         CAST(COALESCE(SUM(CASE e.side WHEN 'credit' THEN e.amount_cents END), 0) AS BIGINT) AS c"
        "  FROM transactions t LEFT JOIN entries e ON e.transaction_id = t.id"
        "  GROUP BY t.id"
        ") per_transaction WHERE n < 2 OR d != c ORDER BY id"
    ).fetchall()

    evidence = [
        {"transaction_id": tx, "lines": n, "debits_cents": d, "credits_cents": c} for tx, n, d, c in bad
    ]
    if debits != credits:
        evidence.insert(0, {"scope": "whole ledger", "debits_cents": debits, "credits_cents": credits})
    if not evidence:
        return InvariantResult(
            "ledger_balanced", True, f"debits {format_cents(debits)} = credits {format_cents(credits)}"
        )
    return InvariantResult(
        "ledger_balanced",
        False,
        f"debits {format_cents(debits)}, credits {format_cents(credits)};"
        f" {len(bad)} transaction(s) unbalanced or missing lines",
        evidence,
    )


def _matches_provider(
    events: list[Event], provider_balance: int, ledger_clearing: int, clearing: dict[tuple[str, str], int]
) -> InvariantResult:
    provider_net: dict[str, int] = defaultdict(int)
    for e in events:
        provider_net[e.payment_id] += MONEY_EFFECT[e.type] * e.amount_cents
    ledger_net: dict[str, int] = defaultdict(int)
    for (payment_id, _), net in clearing.items():
        ledger_net[payment_id] += net

    evidence = []
    if ledger_clearing != provider_balance:
        evidence.append(
            {
                "scope": "provider_clearing, all payments",
                "ledger_cents": ledger_clearing,
                "provider_cents": provider_balance,
                "difference_cents": ledger_clearing - provider_balance,
            }
        )
    for payment_id in sorted(set(provider_net) | set(ledger_net)):
        ours, theirs = ledger_net.get(payment_id, 0), provider_net.get(payment_id, 0)
        if ours != theirs:
            evidence.append(
                {"scope": payment_id, "ledger_cents": ours, "provider_cents": theirs, "difference_cents": ours - theirs}
            )

    if not evidence:
        return InvariantResult(
            "matches_provider", True, f"ledger and provider both say {format_cents(provider_balance)}"
        )
    if ledger_clearing == provider_balance:
        summary = f"totals agree at {format_cents(provider_balance)}, but {len(evidence)} payment(s) differ"
    else:
        summary = (
            f"ledger says {format_cents(ledger_clearing)}, provider says {format_cents(provider_balance)}:"
            f" off by {format_cents(ledger_clearing - provider_balance, signed=True)}"
        )
    return InvariantResult("matches_provider", False, summary, evidence)


def _payments_match_provider(
    provider_payments: list[ProviderPayment], db: SqlConnection, cutoff: int | None = None
) -> tuple[InvariantResult, int]:
    """Invariant 4, and how many of the provider's charges it left for the next check: those newer than the cutoff."""

    def newer(charge: ProviderPayment) -> bool:
        return cutoff is not None and charge.created is not None and charge.created > cutoff

    ours = {
        payment_id: (status, amount, provider_id)
        for payment_id, status, amount, provider_id in db.execute(
            "SELECT id, status, amount_cents, provider_payment_id FROM payments ORDER BY id"
        ).fetchall()
    }
    linked = {provider_id: payment_id for payment_id, (_, _, provider_id) in ours.items() if provider_id is not None}
    named = {charge.id: charge.klaxon_payment_id for charge in provider_payments}
    # Our payment id -> the provider's charges for it: each charge goes to the payment its metadata names, or,
    # when the metadata names none of ours, to the payment our records link it to.
    charges: dict[str, list[ProviderPayment]] = defaultdict(list)
    strangers = []
    for charge in provider_payments:
        if charge.klaxon_payment_id in ours:
            charges[charge.klaxon_payment_id].append(charge)
        elif charge.id in linked:
            charges[linked[charge.id]].append(charge)
        else:
            strangers.append(charge)

    evidence = []
    never_charged = left_ours = left_strangers = 0
    for payment_id, (status, amount, provider_id) in ours.items():
        found = charges.get(payment_id, [])
        if len(found) > 1:  # judged whatever the cutoff: no timing explains two charges
            problems = ["charged twice" if len(found) == 2 else f"charged {len(found)} times"]
        elif not found:
            if status != "succeeded":
                never_charged += 1  # pending or failed, and no money moved: our records and the provider agree
                continue
            if provider_id in named:  # the charge exists, but its metadata gives it to another payment of ours
                problems = ["linked to another payment's charge"]
            else:
                problems = ["succeeded but never charged"]
        else:
            [charge] = found
            if newer(charge):  # its answer or its webhook may still be on its way: the next check judges it
                left_ours += 1
                continue
            # Every way this payment disagrees with its one charge, not only the first: a stuck payment that was
            # also charged the wrong amount must not read as "just confirm it".
            problems = []
            if status != "succeeded":
                problems.append(f"{status} but charged")
            if charge.amount_cents != amount:
                problems.append("amount differs")
            if status == "succeeded" and provider_id != charge.id:
                problems.append("linked to another charge")
            if charge.klaxon_payment_id != payment_id:
                problems.append("not named in its charge's metadata")
            if not problems:
                continue
        evidence.append(
            {
                "payment_id": payment_id,
                "status": status,
                "amount_cents": amount,
                "provider_payment_id": provider_id,
                "provider_charges": [{"id": c.id, "amount_cents": c.amount_cents} for c in found],
                "problem": ", ".join(problems),
            }
        )
    for charge in strangers:
        if newer(charge):  # its payment of ours may not be in our snapshot yet
            left_strangers += 1
            continue
        evidence.append(
            {
                "payment_id": charge.klaxon_payment_id,
                "status": None,
                "amount_cents": None,
                "provider_payment_id": None,
                "provider_charges": [{"id": charge.id, "amount_cents": charge.amount_cents}],
                "problem": "charged but not ours",
            }
        )

    left = left_ours + left_strangers
    if not evidence:
        judged = len(ours) - left_ours
        charged = judged - never_charged
        summary = (
            f"all {judged} of our payments agree with the provider ({charged} charged once, {never_charged} never charged)"
            if ours or left
            else "no payments yet, ours or the provider's"
        )
        if left:
            summary += f"; {_n(left, 'charge', 'charges')} newer than the cutoff left for the next check"
        return InvariantResult("payments_match_provider", True, summary), left
    summary = "; ".join(_describe_payment(item, named) for item in evidence)
    return InvariantResult("payments_match_provider", False, summary, evidence), left


def _describe_payment(item: dict, named: dict[str, str | None]) -> str:
    """One evidence item of invariant 4, as a sentence."""
    found, payment_id, problem = item["provider_charges"], item["payment_id"], item["problem"]
    if item["status"] is None:
        charged = f"the provider charged {found[0]['id']} ({format_cents(found[0]['amount_cents'])})"
        if payment_id is None:
            return f"{charged}, and its metadata names no payment of ours"
        return f"{charged} for {payment_id}, which is not in our payments table"
    ours = f"{payment_id} ({format_cents(item['amount_cents'])})"
    if len(found) > 1:
        return f"{ours} was charged {len(found)} times at the provider ({', '.join(c['id'] for c in found)})"
    if problem == "succeeded but never charged":
        return f"{ours} is succeeded in our records, but the provider has no charge for it"
    if problem == "linked to another payment's charge":
        other = named[item["provider_payment_id"]]
        return f"{ours} is linked to {item['provider_payment_id']}, but the provider's metadata gives that charge to {other}"
    [charge] = found
    sentences = []
    if item["status"] != "succeeded":
        sentences.append(
            f"is {item['status']} in our records, but the provider charged it"
            f" ({charge['id']}, {format_cents(charge['amount_cents'])})"
        )
    elif "amount differs" in problem:
        sentences.append(f"was charged {format_cents(charge['amount_cents'])} at the provider ({charge['id']})")
    if "linked to another charge" in problem:
        linked = item["provider_payment_id"] or "no charge"
        sentences.append(f"is linked to {linked}, but the provider's charge for it is {charge['id']}")
    if "not named in its charge's metadata" in problem:
        sentences.append(f"is not named in the metadata of its charge {charge['id']}")
    return f"{ours} " + ", and ".join(sentences)


def _refunds_match_provider(
    provider_refunds: list[ProviderRefund], db: SqlConnection, cutoff: int | None = None
) -> tuple[InvariantResult, int]:
    """Invariant 5, invariant 4's twin for refunds, and how many of the provider's refunds it left for the next
    check. One more thing can be wrong with a refund: the provider refunded another payment than ours."""

    def newer(refund: ProviderRefund) -> bool:
        return cutoff is not None and refund.created is not None and refund.created > cutoff

    # A LEFT JOIN: a refund whose payment is missing must still be judged, not dropped from the check.
    ours = {
        refund_id: (payment_id, status, amount, provider_id, provider_payment_id)
        for refund_id, payment_id, status, amount, provider_id, provider_payment_id in db.execute(
            "SELECT r.id, r.payment_id, r.status, r.amount_cents, r.provider_refund_id, p.provider_payment_id"
            " FROM refunds r LEFT JOIN payments p ON p.id = r.payment_id ORDER BY r.id"
        ).fetchall()
    }
    linked = {
        provider_id: refund_id for refund_id, (_, _, _, provider_id, _) in ours.items() if provider_id is not None
    }
    named = {refund.id: refund.klaxon_refund_id for refund in provider_refunds}
    # Our refund id -> the provider's refunds for it: through the metadata first, then through our link.
    made: dict[str, list[ProviderRefund]] = defaultdict(list)
    strangers = []
    for refund in provider_refunds:
        if refund.klaxon_refund_id in ours:
            made[refund.klaxon_refund_id].append(refund)
        elif refund.id in linked:
            made[linked[refund.id]].append(refund)
        else:
            strangers.append(refund)

    evidence = []
    never_made = left_ours = left_strangers = 0
    for refund_id, (payment_id, status, amount, provider_id, provider_payment_id) in ours.items():
        found = made.get(refund_id, [])
        if len(found) > 1:  # judged whatever the cutoff: no timing explains two refunds
            problems = ["refunded twice" if len(found) == 2 else f"refunded {len(found)} times"]
        elif not found:
            if status != "succeeded":
                never_made += 1  # pending or failed, and no money moved back: our records and the provider agree
                continue
            if provider_id in named:  # the refund exists, but its metadata gives it to another refund of ours
                problems = ["linked to another refund's provider refund"]
            else:
                problems = ["succeeded but never refunded"]
        else:
            [refund] = found
            if newer(refund):  # its answer or its webhook may still be on its way: the next check judges it
                left_ours += 1
                continue
            problems = []  # every problem, as for payments: a stuck refund of the wrong amount is not "just confirm it"
            if status != "succeeded":
                problems.append(f"{status} but refunded")
            if refund.amount_cents != amount:
                problems.append("amount differs")
            if refund.payment_id != provider_payment_id:
                problems.append("refunded another payment")
            if status == "succeeded" and provider_id != refund.id:
                problems.append("linked to another provider refund")
            if refund.klaxon_refund_id != refund_id:
                problems.append("not named in its provider refund's metadata")
            if not problems:
                continue
        evidence.append(
            {
                "refund_id": refund_id,
                "payment_id": payment_id,
                "status": status,
                "amount_cents": amount,
                "provider_refund_id": provider_id,
                "provider_payment_id": provider_payment_id,
                "provider_refunds": [_refund_item(r) for r in found],
                "problem": ", ".join(problems),
            }
        )
    for refund in strangers:
        if newer(refund):  # its refund of ours may not be in our snapshot yet
            left_strangers += 1
            continue
        evidence.append(
            {
                "refund_id": refund.klaxon_refund_id,
                "payment_id": None,
                "status": None,
                "amount_cents": None,
                "provider_refund_id": None,
                "provider_payment_id": None,
                "provider_refunds": [_refund_item(refund)],
                "problem": "refunded but not ours",
            }
        )

    left = left_ours + left_strangers
    if not evidence:
        judged = len(ours) - left_ours
        summary = (
            f"all {judged} of our refunds agree with the provider"
            f" ({judged - never_made} refunded once, {never_made} never refunded)"
            if ours or left
            else "no refunds yet, ours or the provider's"
        )
        if left:
            summary += f"; {_n(left, 'refund', 'refunds')} newer than the cutoff left for the next check"
        return InvariantResult("refunds_match_provider", True, summary), left
    summary = "; ".join(_describe_refund(item, named) for item in evidence)
    return InvariantResult("refunds_match_provider", False, summary, evidence), left


def _refund_item(refund: ProviderRefund) -> dict:
    return {"id": refund.id, "provider_payment_id": refund.payment_id, "amount_cents": refund.amount_cents}


def _describe_refund(item: dict, named: dict[str, str | None]) -> str:
    """One evidence item of invariant 5, as a sentence."""
    found, refund_id, problem = item["provider_refunds"], item["refund_id"], item["problem"]
    if item["status"] is None:
        refunded = f"the provider refunded {found[0]['id']} ({format_cents(found[0]['amount_cents'])})"
        if refund_id is None:
            return f"{refunded}, and its metadata names no refund of ours"
        return f"{refunded} for {refund_id}, which is not in our refunds table"
    ours = f"{refund_id} ({format_cents(item['amount_cents'])}, a refund of {item['payment_id']})"
    if len(found) > 1:
        return f"{ours} was refunded {len(found)} times at the provider ({', '.join(r['id'] for r in found)})"
    if problem == "succeeded but never refunded":
        return f"{ours} is succeeded in our records, but the provider has no refund for it"
    if problem == "linked to another refund's provider refund":
        other = named[item["provider_refund_id"]]
        return f"{ours} is linked to {item['provider_refund_id']}, but the provider's metadata gives that refund to {other}"
    [refund] = found
    sentences = []
    if item["status"] != "succeeded":
        sentences.append(
            f"is {item['status']} in our records, but the provider refunded it"
            f" ({refund['id']}, {format_cents(refund['amount_cents'])})"
        )
    elif "amount differs" in problem:
        sentences.append(f"was refunded {format_cents(refund['amount_cents'])} at the provider ({refund['id']})")
    if "refunded another payment" in problem:
        sentences.append(
            f"is for {item['provider_payment_id'] or 'a payment with no provider id in our records'}, but the provider's"
            f" refund {refund['id']} is for {refund['provider_payment_id'] or 'no plain payment id'}"
        )
    if "linked to another provider refund" in problem:
        linked = item["provider_refund_id"] or "no refund"
        sentences.append(f"is linked to {linked}, but the provider's refund for it is {refund['id']}")
    if "not named in its provider refund's metadata" in problem:
        sentences.append(f"is not named in the metadata of its provider refund {refund['id']}")
    return f"{ours} " + ", and ".join(sentences)


def _clearing_by_event(ledger: SqlConnection) -> dict[tuple[str, str], int]:
    """The ledger's net debit to provider_clearing, per (payment id, event id), reversals included."""
    return {
        (payment_id, event_id): net
        for payment_id, event_id, net in ledger.execute(
            "SELECT t.payment_id, t.event_id,"
            "       CAST(SUM(CASE e.side WHEN 'debit' THEN e.amount_cents ELSE -e.amount_cents END) AS BIGINT)"
            " FROM transactions t JOIN entries e ON e.transaction_id = t.id"
            " WHERE e.account = 'provider_clearing' GROUP BY t.payment_id, t.event_id"
        ).fetchall()
    }
