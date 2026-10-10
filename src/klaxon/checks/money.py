"""The money check: does our ledger agree with what really happened at the provider?

Klaxon will run this after every fix, next to the health checks, because a
system can look healthy while the money is wrong.

Three invariants, each with evidence:
1. posted_exactly_once: every provider money event is in the ledger exactly
   once (0 times = lost, 2 or more = counted twice), and the ledger has nothing
   the provider never sent. A posting cancelled by a reversal does not count.
2. ledger_balanced: total debits equal total credits, and every transaction
   has at least two lines and balances on its own.
3. matches_provider: the ledger's provider_clearing balance equals what the
   provider says it holds, overall and per payment.

Invariant 2 alone is not enough: a double posting is itself balanced.

The check reads the ledger with its own SELECT queries, not through the Ledger
class or the handler, so it does not depend on their bookkeeping being right.
The same queries run on the first piece's SQLite ledger and on the live
PostgreSQL ledger (week 1), where the check connects through a read-only role
(see `klaxon.checks.live`). Sums are cast to BIGINT so both return plain ints.
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

    def invariant(self, name: str) -> InvariantResult:
        return next(i for i in self.invariants if i.name == name)

    def to_dict(self) -> dict:
        return asdict(self)


def format_cents(cents: int, signed: bool = False) -> str:
    sign = "-" if cents < 0 else ("+" if signed and cents > 0 else "")
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def check_money(provider: ProviderRecords, ledger: SqlConnection) -> MoneyReport:
    events = [e for e in provider.events if e.type in MONEY_EFFECT]
    provider_balance = provider.balance_cents()
    ledger_clearing = _clearing_net_debit(ledger)
    invariants = [
        _posted_exactly_once(events, ledger),
        _ledger_balanced(ledger),
        _matches_provider(events, provider_balance, ledger_clearing, ledger),
    ]
    return MoneyReport(
        passed=all(i.passed for i in invariants),
        ledger_clearing_cents=ledger_clearing,
        provider_balance_cents=provider_balance,
        money_off_cents=ledger_clearing - provider_balance,
        invariants=invariants,
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


def _posted_exactly_once(events: list[Event], ledger: SqlConnection) -> InvariantResult:
    postings = _live_postings(ledger)
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
        return InvariantResult("posted_exactly_once", True, f"all {len(events)} provider events posted exactly once")
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
    events: list[Event], provider_balance: int, ledger_clearing: int, ledger: SqlConnection
) -> InvariantResult:
    provider_net: dict[str, int] = defaultdict(int)
    for e in events:
        provider_net[e.payment_id] += MONEY_EFFECT[e.type] * e.amount_cents
    ledger_net = dict(
        ledger.execute(
            "SELECT t.payment_id,"
            "       CAST(SUM(CASE e.side WHEN 'debit' THEN e.amount_cents ELSE -e.amount_cents END) AS BIGINT)"
            " FROM transactions t JOIN entries e ON e.transaction_id = t.id"
            " WHERE e.account = 'provider_clearing' GROUP BY t.payment_id"
        )
    )

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


def _clearing_net_debit(ledger: SqlConnection) -> int:
    (net,) = ledger.execute(
        "SELECT CAST(COALESCE(SUM(CASE side WHEN 'debit' THEN amount_cents ELSE -amount_cents END), 0) AS BIGINT)"
        " FROM entries WHERE account = 'provider_clearing'"
    ).fetchone()
    return net
