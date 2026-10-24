"""Faults with a known cause, injected on purpose into this Mac's stack, so Klaxon can be graded on them.

A test harness: not part of the patient and not the Klaxon agent. It only ever acts on this Mac's own
Compose project (see klaxon.patient.stack) and the fake provider at 127.0.0.1.

Each fault says what breaks, the root cause a correct diagnosis must name, what it looks like, and what
fixes it. inject() causes it; clear() removes the cause: a rollback of a bad deploy, or the end of an
outage. Damage already done stays done: money booked twice stays booked twice until someone repairs it
with reversals (week 4, with a human's approval).

    duplicate-delivery  a deploy of the ledger consumer without its dedupe
    poison-message      a deploy that retries unreadable messages forever, then one such message
    provider-outage     the provider's charge API hangs, and our payments API's circuit breaker fails fast
    money-unit          a deploy of the payments API that charges 100 times the amount (week 4)
    secret-rotation     a deploy of the webhook receiver with a secret the provider does not sign with (week 4)
    pool-exhaustion     a deploy of the payments API that never gives its database connections back (week 4)

Fault 3's recorded cause has a version 2 (week 4, item 3): the payments API has had a circuit breaker since, on by
default, so after its first seconds the outage no longer makes payments wait. Each fault keeps what it recorded for an
earlier version of the grading rule (klaxon.grading), so a comparison made then can be made again the same way.

All but the outage are bad deploys: one container is recreated with a known-bad setting, the way the
week 1 demos use HANDLER=naive (klaxon.patient.deploys lists them). They need traffic to show (make
traffic): fault 1 needs the duplicate webhooks that lost acknowledgements cause, which the traffic
generator's noise provides, and fault 6 needs refunds.

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
from klaxon.patient.services.provider_client import BREAKER_FAILURES, BREAKER_OPEN_SECONDS
from klaxon.traffic import require_local

LEDGER = "ledger-consumer"
GOOD_LEDGER = {"LEDGER_HANDLER": "idempotent", "LEDGER_BAD_EVENTS": "dead_letter"}
OUTAGE_DELAY_SECONDS = 5.0  # longer than the payments API's 3 s timeout to the provider
API = "payments-api"
GOOD_API = {"PROVIDER_BREAKER": "on", "CHARGE_AMOUNT_UNIT": "cents", "REFUND_CONNECTION": "returned"}
RECEIVER = "webhook-receiver"
GOOD_RECEIVER = {"WEBHOOK_SECRET_VERSION": "v1"}


class LocalStack:
    """What the faults may touch: this Mac's Compose stack and the fake provider's simulator controls."""

    def __init__(
        self,
        provider_url: str = PROVIDER_URL,
        compose: Callable[..., str] = stack.compose,
        ensure_local: Callable[[], None] = stack.ensure_local,
        http: httpx.Client | None = None,
        deployer: deploys.Deployer | None = None,
        inspect: Callable[..., list[dict]] = stack.inspect,
    ) -> None:
        require_local(provider_url)
        # trust_env=False: no proxy from the environment or the system settings, so nothing leaves this Mac.
        self.http = http or httpx.Client(base_url=provider_url, timeout=15.0, trust_env=False)
        require_local(str(self.http.base_url))
        self.deployer = deployer or deploys.Deployer(compose=compose, inspect=inspect, ensure_local=ensure_local)

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
class Recorded:
    """A cause and a remedy, as a fault recorded them for one version of the grading rule."""

    cause: str
    remedy: str


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
    # What it recorded for earlier versions of the grading rule, where they differ from today's: by rubric version.
    earlier: dict[int, Recorded] = field(default_factory=dict, repr=False)

    def recorded(self, rubric_version: int) -> Recorded:
        """The cause and the remedy a comparison by this version of the grading rule reads."""
        return self.earlier.get(rubric_version, Recorded(self.cause, self.remedy))

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


def _deploy_api(**bad: str) -> Callable[[LocalStack], dict]:
    """A bad deploy of the payments API: its good settings, but for these."""

    def inject(s: LocalStack) -> dict:
        s.redeploy(API, GOOD_API | bad)
        return {"redeployed": API} | bad

    return inject


def _roll_api_back(s: LocalStack) -> dict:
    s.redeploy(API, GOOD_API)
    return {"redeployed": API} | GOOD_API


def _deploy_rotated_secret(s: LocalStack) -> dict:
    s.redeploy(RECEIVER, GOOD_RECEIVER | {"WEBHOOK_SECRET_VERSION": "v2"})
    return {"redeployed": RECEIVER, "WEBHOOK_SECRET_VERSION": "v2"}


def _roll_receiver_back(s: LocalStack) -> dict:
    s.redeploy(RECEIVER, GOOD_RECEIVER)
    return {"redeployed": RECEIVER} | GOOD_RECEIVER


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
            title="Payments fail: the provider hangs, and our circuit breaker fails fast",
            cause=(
                f"the provider's charge and refund API hangs ({OUTAGE_DELAY_SECONDS:g} s, then 503): our payments API's"
                " calls to it time out, each after 3 attempts, until its circuit breaker opens, after"
                f" {BREAKER_FAILURES} calls in a row got no answer; from then on it fails fast, answering 503 'outcome"
                " unknown' at once without calling the provider, and lets one trial call through"
                f" {BREAKER_OPEN_SECONDS:g} s after it opened, and again after each trial that fails"
            ),
            symptoms=(
                "clients get 503 'outcome unknown', most of them at once: the 5xx share of POST /payments climbs",
                "the first calls still wait out their 3 timed-out attempts (about 10 s) until the breaker opens; after"
                f" that only the trial calls wait, the first {BREAKER_OPEN_SECONDS:g} s after it opened, then one"
                f" {BREAKER_OPEN_SECONDS:g} s after each that fails: the p95 latency may climb for about a minute, then"
                " drops",
                "provider calls end with no answer (timeouts) at first, klaxon.provider.attempts result no_answer;"
                " then the breaker refuses them, klaxon.provider.refused",
                "the payments API logs 'circuit breaker open', and its health check keeps passing: once the breaker"
                " opens, its worker threads are free again",
                "the money check passes: nothing was charged, so no money is wrong",
                "after it ends, the next trial call gets an answer and the breaker closes; payments and refunds whose"
                " outcome stayed unknown stay pending, with nothing charged or refunded: they need a decision (retry"
                " them with their keys, or mark them failed), and until then such a refund blocks new refunds on its"
                " payment",
            ),
            remedy=(
                "nothing to change on our side: the provider must recover (escalate to it). Meanwhile the circuit"
                " breaker keeps the API answering at once, and closes again when a trial call gets an answer. After"
                " it recovers, the payments and refunds left pending need a decision (retry them with their keys, or"
                " mark them failed): a refund left pending blocks new refunds on its payment until it settles"
            ),
            expected_money={"during": "passed", "after": "passed", "quiet": "passed"},  # nothing was charged
            inject=_start_outage,
            clear=_end_outage,
            earlier={  # version 1, from before the circuit breaker: the week 3 runs were graded against it
                1: Recorded(
                    cause=(
                        f"the provider's charge and refund API hangs ({OUTAGE_DELAY_SECONDS:g} s, then 503), and our"
                        " payments API has no circuit breaker: every payment waits through 3 timed-out attempts, the"
                        " API's worker threads fill up, and even its health checks slow down"
                    ),
                    remedy=(
                        "it ends when the provider recovers; a circuit breaker would fail fast meanwhile and keep the"
                        " API answering (planned for week 4)"
                    ),
                )
            },
        ),
        Fault(
            name="money-unit",
            title="Customers charged 100 times the amount: a money-unit bug",
            cause=(
                "a deploy of the payments API with a money-unit bug (CHARGE_AMOUNT_UNIT=dollars): it reads each"
                " payment's amount, already in cents, as dollars and converts it to cents again, so the provider"
                " charges every customer 100 times the amount, while our own records keep the right one"
            ),
            symptoms=(
                "the money check fails on payments_match_provider: for each payment since the deploy, the provider"
                " charged 100 times our amount",
                "the ledger books what the provider charged, so it agrees with the provider, and the other invariants"
                " pass",
                "the payment confirmer logs a conflict for each such payment: the provider charged a different amount"
                " than our payment's",
                "the API stays fast and healthy, and its answers show the right amount",
                "refunds send the right amount: a full refund gives back 1% of what was charged",
                "a payment whose answer was lost during the fault stays pending for good: the confirmer refuses the"
                " provider's 100 times amount as a conflict",
            ),
            remedy=(
                "roll the payments API back to CHARGE_AMOUNT_UNIT=cents, then refund each customer charged since the"
                " deploy what was charged above the amount, at the provider: our API refunds no more than our"
                " recorded amount (a write, so it needs approval)"
            ),
            expected_money={"during": "failed", "after": "failed", "quiet": "failed"},  # the overcharges stay
            inject=_deploy_api(CHARGE_AMOUNT_UNIT="dollars"),
            clear=_roll_api_back,
        ),
        Fault(
            name="secret-rotation",
            title="The books stop catching up: our webhook receiver refuses every webhook",
            cause=(
                "a deploy of the webhook receiver with a rotated secret the provider does not sign with"
                " (WEBHOOK_SECRET_VERSION=v2, while the provider signs with v1): every webhook fails its signature"
                " check and is refused with a 400, so no event reaches Kafka, and the provider keeps sending them"
                " again, with backoff"
            ),
            symptoms=(
                "the books cannot catch up with the provider: the money check cannot settle, its webhooks not"
                " acknowledged",
                "the receiver counts every webhook as bad_signature, and logs 'webhook refused: bad signature'",
                "nothing new is booked or confirmed, and both consumers' lag stays at 0: nothing reaches Kafka",
                "payments whose answer was lost stay pending, so the payments pending over a minute climb",
                "payments and refunds still succeed, and the API stays fast and healthy",
            ),
            remedy=(
                "roll the webhook receiver back to v1 (or accept both versions until the provider signs with v2): the"
                " provider sends every refused webhook again within a minute, and the books catch up"
            ),
            expected_money={"during": "could not run", "after": "passed", "quiet": "passed"},
            inject=_deploy_rotated_secret,
            clear=_roll_receiver_back,
        ),
        Fault(
            name="pool-exhaustion",
            title="Payments fail: the payments API's database pool runs dry",
            cause=(
                "a deploy of the payments API with a connection leak (REFUND_CONNECTION=kept): reading a payment's"
                " refunds takes a connection from its database pool of 10 and never gives it back, so after about"
                " ten refunds the pool is empty, and every request waits 5 s for a connection, then fails with a 500"
            ),
            symptoms=(
                "POST /payments takes about 5 s and fails with a 500: the 5xx share and the p95 latency climb",
                "the payments API logs 'unhandled error' with PoolTimeout: no connection within 5 s",
                "its health check fails: it cannot get a connection either, while PostgreSQL itself is up",
                "the money check passes: a payment that never got a connection was never charged",
            ),
            remedy=(
                "roll the payments API back to REFUND_CONNECTION=returned: its new container starts with a full pool"
            ),
            expected_money={"during": "passed", "after": "passed", "quiet": "passed"},
            inject=_deploy_api(REFUND_CONNECTION="kept"),
            clear=_roll_api_back,
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
