"""The money check, run against the live Docker Compose stack: all four invariants.

The check compares two views of the same money:
- the provider's records, read through the provider's own API (GET /v1/events,
  GET /v1/payments and GET /v1/balance), the way a payments team pulls its
  provider's reports;
- our ledger and our payments table, read from PostgreSQL as klaxon_readonly,
  a role that can read everything and write nothing.

The comparison only means something once the system has settled: the
provider has no webhook left to deliver, and both consumer groups have
handled every Kafka message (their lag is 0): the ledger by booking it,
skipping it as a duplicate or dead-lettering it; the payment confirmer by
confirming the payment, finding nothing to do, logging a conflict or
dead-lettering the message. Before that, an event still in flight would look
lost, and a payment still waiting for its webhook would look stuck. When a
consumer is stalled on purpose, `--no-wait` runs the check anyway.
So the check waits for all of that, then takes the provider's records, then
reads our tables inside one REPEATABLE READ transaction, so every query sees
the same moment. It also assumes no client is sending requests meanwhile.

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

import httpx
import psycopg
from confluent_kafka import KafkaException

from klaxon.checks.money import MoneyReport, ProviderPayment, check_money
from klaxon.patient.provider import Event
from klaxon.patient.services.events import ID
from klaxon.patient.services.kafka_io import CONFIRMER_GROUP, EVENTS_TOPIC, LEDGER_GROUP, consumer_lag

# This Mac's view of the stack (see the ports in compose.yaml). Local development values, not secrets.
# 127.0.0.1, not localhost: localhost tries IPv6 first, and the ports listen on IPv4 only.
PROVIDER_URL = os.environ.get("KLAXON_PROVIDER_URL", "http://127.0.0.1:8081")
READONLY_DSN = os.environ.get(
    "KLAXON_READONLY_DSN", "postgresql://klaxon_readonly:klaxon-readonly-dev-only@127.0.0.1:5432/klaxon"
)
KAFKA_BOOTSTRAP = os.environ.get("KLAXON_KAFKA_BOOTSTRAP", "127.0.0.1:9094")
# Both consumer groups read provider-events. The check waits until both have caught up.
CONSUMER_GROUPS = (LEDGER_GROUP, CONFIRMER_GROUP)


class ProviderAPI:
    """The provider's records, fetched once: a snapshot. Fits the money check's ProviderRecords."""

    def __init__(self, base_url: str = PROVIDER_URL, http: httpx.Client | None = None, page_size: int = 100) -> None:
        self.http = http or httpx.Client(base_url=base_url, timeout=10.0)
        self.events = [
            Event(item["id"], item["type"], item["data"]["payment_id"], item["data"]["amount_cents"], item["created"])
            for item in self._pages("/v1/events", page_size)
        ]
        self.payments = [
            ProviderPayment(item["id"], item["amount_cents"], our_payment_id(item))
            for item in self._pages("/v1/payments", page_size)
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
        response = self.http.get("/v1/balance")
        response.raise_for_status()
        return response.json()["available_cents"]


def our_payment_id(payment: dict) -> str | None:
    """Our payment id, from a provider payment's metadata.

    Provider output is untrusted input: the evidence goes to an AI agent, and text can carry instructions. So
    anything but a short plain id counts as no id of ours (the check then reports the charge as not ours).
    """
    metadata = payment.get("metadata")
    ours = metadata.get("klaxon_payment_id") if isinstance(metadata, dict) else None
    return ours if isinstance(ours, str) and ID.fullmatch(ours) else None


@dataclass
class Settling:
    settled: bool
    waited_seconds: float
    pending_deliveries: int  # webhooks the provider has not had acknowledged yet
    lag: dict[str, int]  # per consumer group: Kafka messages it has not committed yet
    lag_by_partition: dict[str, dict[int, int]]


def pending_deliveries(provider_url: str = PROVIDER_URL) -> int:
    response = httpx.get(f"{provider_url}/admin/deliveries", timeout=10.0)
    response.raise_for_status()
    return response.json()["stats"]["pending"]


def consumer_lags(
    kafka_bootstrap: str = KAFKA_BOOTSTRAP, lag: Callable[[str, str, str], dict[int, int]] = consumer_lag
) -> dict[str, dict[int, int]]:
    """Per consumer group, per partition: the messages it has not committed yet."""
    return {group: lag(kafka_bootstrap, group, EVENTS_TOPIC) for group in CONSUMER_GROUPS}


def settling_state(
    pending: Callable[[], int] = pending_deliveries, lags: Callable[[], dict[str, dict[int, int]]] = consumer_lags
) -> tuple[int, dict[str, dict[int, int]]]:
    """Read the provider first, then Kafka.

    The order matters. The receiver acknowledges a webhook only after Kafka has it, so once the provider has
    nothing pending, every event is already in Kafka, and a lag of 0 read after that covers all of them. Read
    the other way round, an event could reach Kafka between the two reads and be missed.
    """
    waiting = pending()
    return waiting, lags()


def wait_until_settled(
    timeout_seconds: float = 60.0,
    poll_seconds: float = 0.5,
    state: Callable[[], tuple[int, dict[str, dict[int, int]]]] = settling_state,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Settling:
    """Wait until nothing is pending and no group is lagging, twice in a row (traffic may still be trickling in)."""
    start = clock()
    quiet = 0
    while True:
        pending, lags = state()
        behind = {group: sum(by_partition.values()) for group, by_partition in lags.items()}
        quiet = quiet + 1 if pending == 0 and not any(behind.values()) else 0
        waited = round(clock() - start, 2)
        if quiet >= 2:
            return Settling(True, waited, pending, behind, lags)
        if waited >= timeout_seconds:
            return Settling(False, waited, pending, behind, lags)
        sleep(poll_seconds)


def read_ledger_and_check(provider: ProviderAPI, readonly_dsn: str = READONLY_DSN) -> MoneyReport:
    """All four invariants: the ledger and our payments table against the provider's snapshot."""
    with psycopg.connect(readonly_dsn, connect_timeout=5) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ  # one snapshot for every query
        conn.read_only = True
        return check_money(provider, conn, provider.payments)


@dataclass
class LiveCheck:
    settling: Settling | None
    report: MoneyReport

    def to_dict(self) -> dict:
        return {"settling": asdict(self.settling) if self.settling else None, "money_check": self.report.to_dict()}


def check_live(wait: bool = True, timeout_seconds: float = 60.0) -> LiveCheck:
    settling = wait_until_settled(timeout_seconds) if wait else None
    if settling is not None and not settling.settled:
        raise NotSettled(settling)
    return LiveCheck(settling, read_ledger_and_check(ProviderAPI()))


def describe_lag(lag: dict[str, int]) -> str:
    return ", ".join(f"{group} {n}" for group, n in lag.items())


class NotSettled(Exception):
    def __init__(self, settling: Settling) -> None:
        super().__init__(
            f"not settled after {settling.waited_seconds} s: {settling.pending_deliveries} webhook(s) pending,"
            f" consumer lag {describe_lag(settling.lag)}"
        )
        self.settling = settling


def render(result: LiveCheck) -> str:
    out = ["Money check against the live stack (synthetic data, fake provider)"]
    if result.settling is not None:
        out.append(
            f"Settled after {result.settling.waited_seconds} s: 0 webhooks pending at the provider,"
            f" consumer lag {describe_lag(result.settling.lag)}"
        )
    else:
        out.append("Did not wait for the system to settle (--no-wait); in-flight events can look lost.")
    for inv in result.report.invariants:
        out.append(f"  {'PASS' if inv.passed else 'FAIL'}  {inv.name:<24} {inv.summary}")
    out.append(f"RESULT: money check {'PASSED' if result.report.passed else 'FAILED'}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m klaxon.checks.live", description=__doc__.splitlines()[0])
    parser.add_argument("--no-wait", action="store_true", help="check now, without waiting for the system to settle")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for the system to settle")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    try:
        result = check_live(wait=not args.no_wait, timeout_seconds=args.timeout)
    except NotSettled as exc:
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
