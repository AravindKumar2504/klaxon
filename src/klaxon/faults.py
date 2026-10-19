"""Faults with a known cause, injected on purpose into this Mac's stack, so Klaxon can be graded on them.

A test harness: not part of the patient and not the Klaxon agent. It only ever acts on this Mac's own
Compose project (see klaxon.patient.stack) and the fake provider at 127.0.0.1.

Each fault says what breaks, the root cause a correct diagnosis must name, what it looks like, and what
fixes it. inject() causes it; clear() removes the cause: a rollback of a bad deploy, or the end of an
outage. Damage already done stays done: money booked twice stays booked twice until someone repairs it
with reversals (week 4, with a human's approval).

    duplicate-delivery  a deploy of the ledger consumer without its dedupe
    poison-message      a deploy that retries unreadable messages forever, then one such message
    provider-outage     the provider's charge API hangs, and our payments API has no circuit breaker

The first two are bad deploys: one container is recreated with a known-bad setting, the way the
week 1 demos use HANDLER=naive. They need traffic to show (make traffic): fault 1 needs the duplicate
webhooks that lost acknowledgements cause, which the traffic generator's noise provides.

Run: python -m klaxon.faults list | inject NAME | clear NAME
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from klaxon.checks.live import PROVIDER_URL
from klaxon.patient import deploys, stack
from klaxon.traffic import require_local

LEDGER = "ledger-consumer"
GOOD_LEDGER = {"LEDGER_HANDLER": "idempotent", "LEDGER_BAD_EVENTS": "dead_letter"}
OUTAGE_DELAY_SECONDS = 5.0  # longer than the payments API's 3 s timeout to the provider


class LocalStack:
    """What the faults may touch: this Mac's Compose stack and the fake provider's simulator controls."""

    def __init__(
        self,
        provider_url: str = PROVIDER_URL,
        compose: Callable[..., str] = stack.compose,
        ensure_local: Callable[[], None] = stack.ensure_local,
        http: httpx.Client | None = None,
        deployer: deploys.Deployer | None = None,
    ) -> None:
        require_local(provider_url)
        # trust_env=False: no proxy from the environment or the system settings, so nothing leaves this Mac.
        self.http = http or httpx.Client(base_url=provider_url, timeout=15.0, trust_env=False)
        require_local(str(self.http.base_url))
        self.deployer = deployer or deploys.Deployer(compose=compose, ensure_local=ensure_local)

    def redeploy(self, service: str, env: dict[str, str]) -> None:
        """Recreate one service's container with these settings, wait until it is healthy, and log the deploy
        (klaxon.patient.deploys), so that a rollback can go back to what it replaced. Docker must talk to this Mac.
        Nothing is done when the service already runs these settings."""
        self.deployer.deploy(service, env, by="klaxon.faults")

    def behavior(self) -> dict:
        return self.http.get("/admin/behavior").raise_for_status().json()

    def change_behavior(self, **changes: object) -> dict:
        """Change some of the provider's behavior and keep the rest, such as the traffic's noise."""
        return self.http.put("/admin/behavior", json=self.behavior() | changes).raise_for_status().json()

    def send_malformed_event(self) -> dict:
        return self.http.post("/admin/malformed-event", json={}).raise_for_status().json()


@dataclass(frozen=True)
class Fault:
    name: str
    title: str
    cause: str  # what a correct diagnosis names
    symptoms: tuple[str, ...]  # what it is expected to look like, while it lasts
    remedy: str
    # What the money check should say while the fault is in place, after clearing it, and once the traffic stops:
    # passed, failed, or could not run (it cannot settle).
    expected_money: dict[str, str]
    inject: Callable[[LocalStack], dict] = field(repr=False)
    clear: Callable[[LocalStack], dict] = field(repr=False)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "cause": self.cause,
            "expected_symptoms": list(self.symptoms),
            "expected_money": dict(self.expected_money),
            "remedy": self.remedy,
        }


def _deploy_naive_ledger(s: LocalStack) -> dict:
    s.redeploy(LEDGER, GOOD_LEDGER | {"LEDGER_HANDLER": "naive"})
    return {"redeployed": LEDGER, "LEDGER_HANDLER": "naive"}


def _deploy_retrying_ledger_then_poison(s: LocalStack) -> dict:
    s.redeploy(LEDGER, GOOD_LEDGER | {"LEDGER_BAD_EVENTS": "retry"})
    try:
        poison = s.send_malformed_event()
    except Exception:
        s.redeploy(LEDGER, GOOD_LEDGER)  # never leave half a fault: without the poison, roll the deploy back
        raise
    return {"redeployed": LEDGER, "LEDGER_BAD_EVENTS": "retry", "poison": poison}


def _roll_ledger_back(s: LocalStack) -> dict:
    s.redeploy(LEDGER, GOOD_LEDGER)
    return {"redeployed": LEDGER} | GOOD_LEDGER


def _start_outage(s: LocalStack) -> dict:
    s.change_behavior(outage_delay_seconds=OUTAGE_DELAY_SECONDS)
    return {"provider": f"charges and refunds held {OUTAGE_DELAY_SECONDS:g} s, then 503"}


def _end_outage(s: LocalStack) -> dict:
    s.change_behavior(outage_delay_seconds=0.0)
    return {"provider": "answering again"}


FAULTS = {
    fault.name: fault
    for fault in (
        Fault(
            name="duplicate-delivery",
            title="Payments counted twice in the books",
            cause=(
                "a deploy of the ledger consumer without its dedupe (LEDGER_HANDLER=naive): every webhook the provider"
                " sends again, after an acknowledgement it never got, is booked a second time"
            ),
            symptoms=(
                "the money check fails: events counted twice, the books ahead of the provider by their amounts",
                "the webhook dashboard stays green, and consumer lag stays at 0",
                "the ledger stops reporting 'already processed' while the confirmer still sees the same duplicates",
            ),
            remedy=(
                "roll the ledger consumer back to the idempotent handler, then repair each double posting with a"
                " reversal (a write, so it needs approval)"
            ),
            expected_money={"during": "failed", "after": "failed", "quiet": "failed"},  # the damage stays
            inject=_deploy_naive_ledger,
            clear=_roll_ledger_back,
        ),
        Fault(
            name="poison-message",
            title="The ledger stops booking: a poison message",
            cause=(
                "a deploy of the ledger consumer that retries unreadable messages forever (BAD_EVENTS=retry), then one"
                " unreadable event from the provider (a payment.updated with its amount as a decimal string): the"
                " ledger consumer is stuck on it, and books nothing behind it"
            ),
            symptoms=(
                "the ledger's lag climbs on every partition: one consumer reads all three, and it is stuck on that"
                " one message (head-of-line blocking); the payment confirmer keeps up",
                "the ledger's retries climb, with 'could not book; will retry the same message' errors for one offset",
                "the confirmer dead-letters the same event as unreadable; the ledger never does",
                "the ledger consumer turns unhealthy: its heartbeat stops while it retries",
                "the money check cannot settle (exit 2); without waiting, the events behind the poison look lost",
            ),
            remedy=(
                "roll the ledger consumer back to dead-lettering: the poison goes to the dead-letter topic, the backlog"
                " drains, and the money check passes again"
            ),
            expected_money={"during": "could not run", "after": "passed", "quiet": "passed"},
            inject=_deploy_retrying_ledger_then_poison,
            clear=_roll_ledger_back,
        ),
        Fault(
            name="provider-outage",
            title="Payments time out: the provider hangs, and nothing fails fast",
            cause=(
                f"the provider's charge and refund API hangs ({OUTAGE_DELAY_SECONDS:g} s, then 503), and our payments"
                " API has no circuit breaker: every payment waits through 3 timed-out attempts, the API's worker"
                " threads fill up, and even its health checks slow down"
            ),
            symptoms=(
                "the payments API's p95 latency climbs to about 10 s (the top of its buckets), and clients get 503"
                " 'outcome unknown'",
                "provider calls end with no answer (timeouts): klaxon.provider.attempts, result no_answer",
                "the payments API's health check fails while its worker threads all wait",
                "the money check passes: nothing was charged, so no money is wrong",
                "after it ends, payments whose outcome stayed unknown stay pending, with no charge: they need a"
                " decision (retry them, or mark them failed)",
            ),
            remedy=(
                "it ends when the provider recovers; a circuit breaker would fail fast meanwhile and keep the API"
                " answering (planned for week 4)"
            ),
            expected_money={"during": "passed", "after": "passed", "quiet": "passed"},  # nothing was charged
            inject=_start_outage,
            clear=_end_outage,
        ),
    )
}


def get(name: str) -> Fault:
    if name not in FAULTS:
        raise KeyError(f"no fault {name!r}; the faults are: {', '.join(FAULTS)}")
    return FAULTS[name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m klaxon.faults", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the faults, their causes and their symptoms")
    for command in ("inject", "clear"):
        sub = commands.add_parser(command, help=f"{command} one fault on this Mac's stack")
        sub.add_argument("name", choices=sorted(FAULTS))
    args = parser.parse_args(argv)
    if args.command == "list":
        for fault in FAULTS.values():
            print(f"{fault.name}: {fault.title}\n  cause: {fault.cause}\n  remedy: {fault.remedy}")
            print("".join(f"  expected: {symptom}\n" for symptom in fault.symptoms))
        return 0
    fault = get(args.name)
    try:
        done = (fault.inject if args.command == "inject" else fault.clear)(LocalStack())
    except (httpx.HTTPError, stack.StackError) as exc:
        print(
            f"Could not {args.command} {fault.name}: {exc}. Is the stack up (make up)? To be sure no part of the"
            f" fault is left in place: make fault-clear FAULT={fault.name}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"fault": fault.name, args.command: done}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
