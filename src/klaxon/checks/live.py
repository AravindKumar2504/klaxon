"""The money check, run against the live Docker Compose stack: all five invariants.

The check compares two views of the same money:
- the provider's records, read through the provider's own API (GET /v1/events,
  GET /v1/payments, GET /v1/refunds and GET /v1/balance), the way a payments
  team pulls its provider's reports;
- our ledger and our payments and refunds tables, read from PostgreSQL as
  klaxon_readonly, a role that can read everything and write nothing.

Traffic keeps flowing while it runs (week 2), so it works to a cutoff, the
way payment teams reconcile: a closing time, after which records count for
the next run, like a bank statement's closing date.
1. The cutoff: the second the check starts in, by the provider's clock, which
   stamps every record it makes in whole seconds (like Stripe). The check
   waits for that second to end, so nothing new can get a stamp up to it.
2. Settle up to the cutoff: wait until our receiver has acknowledged every
   webhook of an event up to the cutoff (it acknowledges only once Kafka has
   the event), then note where the topic ends, then wait until both consumer
   groups have handled every message before that point: the ledger by booking
   it, skipping it as a duplicate or dead-lettering it; the payment confirmer
   by confirming the payment or refund, finding nothing to do, logging a
   conflict or dead-lettering it. Newer traffic does not count, so it cannot
   keep the check waiting. When a consumer is stalled on purpose, `--no-wait`
   runs the check anyway; events still in flight then look lost.
3. Read our books, in one REPEATABLE READ snapshot, then the provider's
   records, then its clock again. In that order, everything we booked from
   the provider is already in its records when they are read, so "the
   provider never sent it" is never a matter of timing. And no record can
   be stamped after that clock: one that is, or a clock that went back,
   makes the check refuse to run (exit 2), because such a record would be
   left for "the next check" forever.
4. Judge what the provider did up to the cutoff (klaxon.checks.money). What
   is newer is reported as left for the next check.

One known gap: a webhook attempt that stalls in the network and lands after a
later retry was acknowledged could reach Kafka after "settled". The idempotent
consumers ignore it; it could change a naive-mode result.

Run: python -m klaxon.checks.live    (exit 0 pass, 1 fail, 2 could not run)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import httpx
import psycopg
from confluent_kafka import KafkaException

from klaxon.checks.money import MONEY_EFFECT, MoneyReport, ProviderPayment, ProviderRefund, check_money
from klaxon.patient.provider import Event
from klaxon.patient.services.events import ID
from klaxon.patient.services.kafka_io import CONSUMER_GROUPS, EVENTS_TOPIC, end_offsets, not_yet_handled

# This Mac's view of the stack (see the ports in compose.yaml). Local development values, not secrets.
# 127.0.0.1, not localhost: localhost tries IPv6 first, and the ports listen on IPv4 only.
PROVIDER_URL = os.environ.get("KLAXON_PROVIDER_URL", "http://127.0.0.1:8081")
READONLY_DSN = os.environ.get(
    "KLAXON_READONLY_DSN", "postgresql://klaxon_readonly:klaxon-readonly-dev-only@127.0.0.1:5432/klaxon"
)
KAFKA_BOOTSTRAP = os.environ.get("KLAXON_KAFKA_BOOTSTRAP", "127.0.0.1:9094")
# trust_env=False: no proxy from the environment or the system settings, so these requests never leave this Mac.
HTTP = httpx.Client(timeout=10.0, trust_env=False)
CLOCK_WAIT_LIMIT = 3.0  # seconds; waiting longer for the cutoff's second to end means the provider's clock is wrong


class ClockProblem(Exception):
    """The provider's clock and its stamps disagree, so no cutoff can be trusted. "Could not run", never a pass."""


def _stamp(item: dict, required: bool) -> int | None:
    """A record's created stamp. Provider output is untrusted: anything but whole seconds is refused."""
    created = item.get("created")
    if created is None and not required:
        return None
    if not isinstance(created, int) or isinstance(created, bool) or created < 0:
        raise ValueError(f"the provider sent a created stamp that is not whole seconds: {str(created)[:40]!r}")
    return created


def _required_id(item: dict, key: str) -> str:
    """A provider record's id. As with its stamps, anything but a short plain id is refused: the evidence the check
    writes goes to an AI agent, and text can carry instructions. The text itself is never echoed."""
    value = item.get(key)
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        raise ValueError(f"the provider sent a record whose {key} is not a plain id")
    return value


def _cents(item: dict) -> int:
    value = item.get("amount_cents")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("the provider sent an amount that is not whole cents")
    return value


def _event(item: dict) -> Event:
    """A provider event. Only money events are judged, and reach the evidence, so only theirs are held to plain ids
    and whole cents: an unreadable event of another type (the poison-message fault's) must not stop the check."""
    data = item["data"]
    if item["type"] not in MONEY_EFFECT:
        return Event(item["id"], item["type"], data.get("payment_id"), data.get("amount_cents"), _stamp(item, True))
    return Event(
        _required_id(item, "id"), item["type"], _required_id(data, "payment_id"), _cents(data), _stamp(item, True)
    )


def provider_http(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=10.0, trust_env=False)  # no proxies: this Mac only


class ProviderAPI:
    """The provider's records, fetched once: a snapshot. Fits the money check's ProviderRecords.

    as_of: the balance as of that second (the check's cutoff). Every event, payment and refund is read either way.
    """

    def __init__(
        self,
        base_url: str = PROVIDER_URL,
        http: httpx.Client | None = None,
        page_size: int = 100,
        as_of: int | None = None,
    ) -> None:
        self.http = http or provider_http(base_url)
        self.as_of = as_of
        self.events = [_event(item) for item in self._pages("/v1/events", page_size)]
        self.payments = [
            ProviderPayment(_required_id(item, "id"), _cents(item), our_payment_id(item), _stamp(item, False))
            for item in self._pages("/v1/payments", page_size)
        ]
        self.refunds = [
            ProviderRefund(
                _required_id(item, "id"),
                _required_id(item, "payment_id"),
                _cents(item),
                our_refund_id(item),
                _stamp(item, False),
            )
            for item in self._pages("/v1/refunds", page_size)
        ]
        self._balance = self._fetch_balance()

    def balance_cents(self) -> int:
        return self._balance

    def _pages(self, path: str, page_size: int) -> list[dict]:
        items: list[dict] = []
        after: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": page_size}
            if after is not None:
                params["starting_after"] = after
            response = self.http.get(path, params=params)
            response.raise_for_status()
            page = response.json()
            items += page["data"]
            if not page["has_more"] or not page["data"]:
                return items
            after = page["data"][-1]["id"]

    def _fetch_balance(self) -> int:
        response = self.http.get("/v1/balance", params={} if self.as_of is None else {"as_of": self.as_of})
        response.raise_for_status()
        return response.json()["available_cents"]


def plain_id(value: object) -> str | None:
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def our_payment_id(payment: dict) -> str | None:
    """Our payment id, from a provider payment's metadata.

    Provider output is untrusted input: the evidence goes to an AI agent, and text can carry instructions. So
    anything but a short plain id counts as no id of ours (the check then reports the charge as not ours).
    """
    metadata = payment.get("metadata")
    return plain_id(metadata.get("klaxon_payment_id") if isinstance(metadata, dict) else None)


def our_refund_id(refund: dict) -> str | None:
    """Our refund id, from a provider refund's metadata, under the same rule."""
    metadata = refund.get("metadata")
    return plain_id(metadata.get("klaxon_refund_id") if isinstance(metadata, dict) else None)


@dataclass
class Settling:
    settled: bool
    waited_seconds: float
    cutoff: int  # the provider's clock, unix seconds: settled means settled up to the end of this second
    pending_deliveries: int  # webhooks of events up to the cutoff our receiver has not acknowledged yet
    behind: dict[str, int] | None  # per consumer group: messages up to the cutoff not handled yet (None: not read)
    behind_by_partition: dict[str, dict[int, int]]


def provider_clock(provider_url: str = PROVIDER_URL) -> float:
    response = HTTP.get(f"{provider_url}/admin/clock")
    response.raise_for_status()
    return float(response.json()["now"])


def choose_cutoff(clock: Callable[[], float] = provider_clock, sleep: Callable[[float], None] = time.sleep) -> int:
    """The second the check starts in, by the provider's clock, once that second is over.

    Everything the provider did before the check started is stamped with this second or an earlier one.
    Once its clock has passed the end of the second, nothing new can get a stamp up to it: the records up
    to the cutoff are final. It waits at most a second; a clock that goes back, or does not move, is refused.
    """
    cutoff = int(clock())
    waited = 0.0
    while (now := clock()) < cutoff + 1:
        if now < cutoff or waited > CLOCK_WAIT_LIMIT:
            raise ClockProblem(f"the provider's clock went back or stopped while the check chose its cutoff ({now})")
        sleep(cutoff + 1 - now)
        waited += cutoff + 1 - now
    return cutoff


def require_stamps_before(provider: ProviderAPI, cutoff: int, now: int) -> None:
    """Refuse a cutoff that the provider's own clock contradicts.

    The provider's records were read before its clock said `now`, so none can be stamped later. One that is
    would be newer than every cutoff, and left for "the next check" forever. A clock back below the cutoff
    means it went back, and records stamped up to the cutoff could still be on their way.
    """
    if now < cutoff + 1:
        raise ClockProblem(f"the provider's clock went back during the check (cutoff {cutoff}, clock {now})")
    late = [e.id for e in provider.events if e.created > now]
    late += [p.id for p in provider.payments if p.created is not None and p.created > now]
    late += [r.id for r in provider.refunds if r.created is not None and r.created > now]
    if late:
        example = late[0] if ID.fullmatch(str(late[0])) else "(an id that is not a plain id)"
        raise ClockProblem(
            f"the provider stamped {len(late)} record(s) after its own clock ({now}), for example {example}:"
            " the cutoff cannot be trusted with them"
        )


def pending_deliveries(up_to: int, provider_url: str = PROVIDER_URL, get: Callable = HTTP.get) -> int:
    """Webhooks of events up to the cutoff that our receiver has not acknowledged yet."""
    response = get(f"{provider_url}/admin/deliveries", params={"created_lte": up_to}, timeout=10.0)
    response.raise_for_status()
    return response.json()["stats"]["pending"]


def topic_end(kafka_bootstrap: str = KAFKA_BOOTSTRAP) -> dict[int, int]:
    """Where provider-events ends now: every message written so far is below it."""
    return end_offsets(kafka_bootstrap, EVENTS_TOPIC)


def groups_not_yet_handled(
    below: dict[int, int], kafka_bootstrap: str = KAFKA_BOOTSTRAP, left: Callable = not_yet_handled
) -> dict[str, dict[int, int]]:
    """Per consumer group, per partition: messages below that point it has not handled yet. Every group counts."""
    return {group: left(kafka_bootstrap, group, EVENTS_TOPIC, below) for group in CONSUMER_GROUPS}


def wait_until_settled(
    cutoff: int,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 0.25,
    pending: Callable[[int], int] = pending_deliveries,
    ends: Callable[[], dict[int, int]] = topic_end,
    left: Callable[[dict[int, int]], dict[str, dict[int, int]]] = groups_not_yet_handled,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Settling:
    """Wait until everything up to the cutoff is handled on our side: first at the provider, then in Kafka.

    The order matters. Once every webhook up to the cutoff is acknowledged, every such event is already in
    Kafka, below the point the topic ends at after that; so that point is read only then, and only once.
    Both conditions only ever move one way (an acknowledged webhook stays acknowledged, a committed offset
    stays committed), so seeing each once is enough, however much new traffic arrives meanwhile.
    """
    start = clock()
    waiting, below = 0, None
    behind: dict[str, int] | None = None
    by_partition: dict[str, dict[int, int]] = {}
    while True:
        if below is None:
            waiting = pending(cutoff)
            if waiting == 0:
                below = ends()
        if below is not None:
            by_partition = left(below)
            behind = {group: sum(partitions.values()) for group, partitions in by_partition.items()}
            if not any(behind.values()):
                return Settling(True, round(clock() - start, 2), cutoff, 0, behind, by_partition)
        waited = round(clock() - start, 2)  # after this round's reads, which can be slow when Kafka is down
        if waited >= timeout_seconds:
            return Settling(False, waited, cutoff, waiting, behind, by_partition)
        sleep(poll_seconds)


def settle(timeout_seconds: float = 60.0) -> Settling:
    """Wait until everything the provider did up to now is handled on our side. For the demos' steps."""
    return wait_until_settled(choose_cutoff(), timeout_seconds)


def read_ledger_and_check(
    cutoff: int | None,
    readonly_dsn: str = READONLY_DSN,
    read_provider: Callable[[int | None], ProviderAPI] = lambda as_of: ProviderAPI(as_of=as_of),
    clock: Callable[[], float] = provider_clock,
) -> tuple[ProviderAPI, MoneyReport]:
    """Our books first, in one snapshot; then the provider's records; then all five invariants, to the cutoff."""
    with psycopg.connect(readonly_dsn, connect_timeout=5) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ  # one snapshot for every query
        conn.read_only = True
        # PostgreSQL takes the snapshot at the first query, so this one fixes it before the provider is read.
        conn.execute("SELECT count(*) FROM transactions").fetchone()
        provider = read_provider(cutoff)
        if cutoff is not None:
            require_stamps_before(provider, cutoff, int(clock()))
        return provider, check_money(
            provider, conn, provider.payments, cutoff=cutoff, provider_refunds=provider.refunds
        )


@dataclass
class LiveCheck:
    cutoff: int
    settling: Settling | None
    provider: ProviderAPI | None
    report: MoneyReport

    def to_dict(self) -> dict:
        return {
            "cutoff": self.cutoff,
            "settling": asdict(self.settling) if self.settling else None,
            "money_check": self.report.to_dict(),
        }


def check_live(wait: bool = True, timeout_seconds: float = 60.0) -> LiveCheck:
    cutoff = choose_cutoff()
    settling = wait_until_settled(cutoff, timeout_seconds) if wait else None
    if settling is not None and not settling.settled:
        raise NotSettled(settling)
    provider, report = read_ledger_and_check(cutoff)
    return LiveCheck(cutoff, settling, provider, report)


def describe_behind(behind: dict[str, int]) -> str:
    return ", ".join(f"{group} {n}" for group, n in behind.items())


class NotSettled(Exception):
    def __init__(self, settling: Settling) -> None:
        if settling.behind is None:
            where = f"{settling.pending_deliveries} webhook(s) up to it not acknowledged by our receiver yet"
        else:
            where = f"messages up to it not handled yet: {describe_behind(settling.behind)}"
        super().__init__(f"not settled up to the cutoff after {settling.waited_seconds} s: {where}")
        self.settling = settling


def utc(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def render(result: LiveCheck) -> str:
    out = [
        "Money check against the live stack (synthetic data, fake provider)",
        f"Cutoff {utc(result.cutoff)} (the provider's clock): checked up to the end of that second",
    ]
    left = result.report.left_for_next_check
    if left.get("provider_events") or left.get("provider_charges") or left.get("provider_refunds"):
        out.append(
            f"  Left for the next check, newer than the cutoff: {left.get('provider_events', 0)} provider event(s),"
            f" {left.get('provider_charges', 0)} charge(s), {left.get('provider_refunds', 0)} refund(s)"
        )
    if result.settling is not None:
        out.append(
            f"Settled up to the cutoff after {result.settling.waited_seconds} s: every webhook up to it acknowledged,"
            " and both consumer groups have handled every message up to it"
        )
    else:
        out.append("Did not wait for the system to settle (--no-wait); events still in flight can look lost.")
    for inv in result.report.invariants:
        out.append(f"  {'PASS' if inv.passed else 'FAIL'}  {inv.name:<24} {inv.summary}")
    out.append(f"RESULT: money check {'PASSED' if result.report.passed else 'FAILED'}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m klaxon.checks.live", description=__doc__.splitlines()[0])
    parser.add_argument("--no-wait", action="store_true", help="check without waiting to settle up to the cutoff")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait to settle up to the cutoff")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    try:
        result = check_live(wait=not args.no_wait, timeout_seconds=args.timeout)
    except (NotSettled, ClockProblem) as exc:
        print(f"Could not run the money check: {exc}", file=sys.stderr)
        return 2
    except (httpx.HTTPError, psycopg.Error, KafkaException, OSError) as exc:
        print(f"Could not reach the stack (is it up? make up): {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # anything before a result exists means "could not run" (2), never "failed" (1)
        print(f"Could not run the money check: {exc!r}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), indent=2) if args.json else render(result))
    return 0 if result.report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
