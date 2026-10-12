"""Demo: the provider charges a payment, but its answer never reaches us. Is the payment confirmed?

A real run on the live Docker Compose stack (synthetic data, simulated provider).
Five payments, with the first piece's customers and amounts for the seed. For
the third, the fake provider saves the charge, queues its webhook, and then
answers our payments API with a 500, to every retry with the same key. (Stripe
documents the same: it replays a key's first answer, 500 errors included.) The
API cannot know whether the customer was charged, so it answers the client 503
and leaves the payment pending. The client gives up. Later, someone asks to
refund half of that payment.

- With the payment confirmer (CONFIRMER_HANDLER=by_our_id), the provider's
  webhook confirms the payment through our id in its metadata. It ends
  succeeded, the refund goes through, and all four invariants pass.
- With the known-bad confirmer (by_provider_id), the payment stays pending
  and the refund is refused. The webhook endpoint stays green, and the ledger
  balances and matches the provider, because it booked the charge from the
  webhook. Only the fourth invariant fails.

The scenario runs on any clean stack: it first waits until the stack has
settled and the money check passes, so whatever fails afterwards was caused by
the scenario. `make demo-lost-answer` starts from a fresh stack, so the ids in
its evidence start at pmt_000001.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from klaxon.checks.live import PROVIDER_URL, ProviderAPI, Settling, read_ledger_and_check, wait_until_settled
from klaxon.checks.money import MoneyReport, format_cents
from klaxon.demo.live import (
    API_URL,
    DemoError,
    ledger_transactions,
    our_payments,
    provenance,
    scenario_from_first_piece,
)
from klaxon.patient import stack
from klaxon.patient.dashboard import Dashboard, summarize
from klaxon.patient.ledger import RequestRecord

SCENARIO = "live-lost-answer"
LABEL = (
    "Real run on this Mac's Docker Compose stack: payments API, fake provider, webhook receiver, Kafka,"
    " ledger consumer, payment confirmer, PostgreSQL and Redis. Synthetic data and a simulated provider: no real"
    " money, no real payment company. The lost answer is injected on purpose through the simulator's controls."
    " It shows the money check Klaxon will use to verify fixes; it is not the Klaxon agent."
)
CONFIRMERS = {
    "by_our_id": "finds the payment by our own id, from the metadata the provider sends back",
    "by_provider_id": "the known-bad baseline: finds the payment by the provider's id, which a lost answer never gave us",
}


@dataclass
class LostAnswerRun:
    confirmer: str
    container_confirmer: str
    seed: int
    run_id: str  # in every idempotency key, so the scenario can run again on the same stack
    lost_position: int  # which payment's answer is lost, 1-based
    stuck_payment_id: str  # our id for that payment
    requests: list[dict]  # what we sent the payments API, and what it answered
    after_webhooks: dict  # the payments API's view of that payment once every webhook was handled
    refund: dict  # the refund we then asked for, and the answer
    settling: Settling
    attempts: list[dict]  # webhook delivery attempts for this scenario's events
    provider: ProviderAPI
    report: MoneyReport
    dashboard: Dashboard
    ledger_transactions: list[dict]
    our_payments: list[dict]

    @property
    def api_answers(self) -> dict[int, int]:
        """Status codes our payments API gave the client, and how many times each."""
        return dict(sorted(Counter(r["status"] for r in [*self.requests, self.refund]).items()))


def clean_start(settle_timeout: float) -> ProviderAPI:
    """Wait until settled, then require a passing money check. Returns the provider's records at that moment."""
    settling = wait_until_settled(settle_timeout)
    if not settling.settled:
        raise DemoError(f"the stack did not settle within {settle_timeout} s before the scenario: {settling}")
    snapshot = ProviderAPI(PROVIDER_URL)
    before = read_ledger_and_check(snapshot)
    if not before.passed:
        failed = ", ".join(i.name for i in before.invariants if not i.passed)
        raise DemoError(f"the money check fails before the scenario starts ({failed}): make reset, then make up")
    return snapshot


def run_live(
    confirmer: str, seed: int = 7, payments: int = 5, lost_position: int = 3, settle_timeout: float = 60.0
) -> LostAnswerRun:
    if confirmer not in CONFIRMERS:
        raise DemoError(f"unknown confirmer {confirmer!r}")
    if not 1 <= lost_position <= payments:
        raise DemoError(f"the lost answer must be one of the {payments} payments")
    container_confirmer = stack.container_env("payment-confirmer").get("CONFIRMER_HANDLER", "by_our_id")
    if container_confirmer != confirmer:
        raise DemoError(
            f"the running payment confirmer uses {container_confirmer!r}, not {confirmer!r}: make up CONFIRMER={confirmer}"
        )
    customers = scenario_from_first_piece(seed, payments).payments
    run_id = uuid.uuid4().hex[:8]

    with (
        httpx.Client(base_url=PROVIDER_URL, timeout=10.0) as provider,
        httpx.Client(base_url=API_URL, timeout=30.0) as api,
    ):
        known_events = {e.id for e in clean_start(settle_timeout).events}
        provider.put("/admin/behavior", json={}).raise_for_status()  # nothing else misbehaves in this run

        requests, stuck = [], None
        for n, (customer, amount) in enumerate(customers, start=1):
            lost = n == lost_position
            if lost:
                provider.put("/admin/behavior", json={"fail_after_charge_rate": 1.0}).raise_for_status()
            try:
                key = f"lost-answer-{run_id}-payment-{n}"
                response = api.post(
                    "/payments", json={"customer": customer, "amount_cents": amount}, headers={"Idempotency-Key": key}
                )
            finally:
                if lost:
                    provider.put("/admin/behavior", json={}).raise_for_status()
            expected = 503 if lost else 201
            if response.status_code != expected:
                raise DemoError(f"payment {n}: expected {expected}, got {response.status_code} {response.text[:300]}")
            body = response.json()
            payment = body["payment"] if lost else body
            requests.append(
                {"call": "POST /payments", "idempotency_key": key, "status": response.status_code, "body": body}
            )
            if lost:
                stuck = payment

        settling = wait_until_settled(settle_timeout)
        if not settling.settled:
            raise DemoError(f"the stack did not settle within {settle_timeout} s: {settling}")
        after_webhooks = api.get(f"/payments/{stuck['id']}").raise_for_status().json()

        key = f"lost-answer-{run_id}-refund-1"
        response = api.post(
            f"/payments/{stuck['id']}/refunds",
            json={"amount_cents": stuck["amount_cents"] // 2},
            headers={"Idempotency-Key": key},
        )
        if response.status_code not in (201, 409):
            raise DemoError(f"the refund got {response.status_code} {response.text[:300]}")
        refund = {
            "call": f"POST /payments/{stuck['id']}/refunds",
            "idempotency_key": key,
            "status": response.status_code,
            "body": response.json(),
        }

        settling = wait_until_settled(settle_timeout)
        if not settling.settled:
            raise DemoError(f"the stack did not settle within {settle_timeout} s after the refund: {settling}")
        attempts = provider.get("/admin/deliveries", params={"attempts": "true"}).raise_for_status().json()["attempts"]

    snapshot = ProviderAPI(PROVIDER_URL)
    attempts = [a for a in attempts if a["event_id"] not in known_events]
    return LostAnswerRun(
        confirmer=confirmer,
        container_confirmer=container_confirmer,
        seed=seed,
        run_id=run_id,
        lost_position=lost_position,
        stuck_payment_id=stuck["id"],
        requests=requests,
        after_webhooks=after_webhooks,
        refund=refund,
        settling=settling,
        attempts=attempts,
        provider=snapshot,
        report=read_ledger_and_check(snapshot),
        # What HTTP monitoring of our webhook endpoint would see: every status code it answered in this scenario.
        dashboard=summarize(
            RequestRecord(a["event_id"], a["status"], "answered") for a in attempts if a["status"] is not None
        ),
        ledger_transactions=ledger_transactions(),
        our_payments=our_payments(),
    )


def verdict(run: LostAnswerRun) -> str:
    report, dashboard = run.report, run.dashboard
    if report.passed:
        return f"RESULT: money check PASSED (dashboard {dashboard.status}, all four invariants hold)"
    failed = ", ".join(i.name for i in report.invariants if not i.passed)
    ledger_ok = all(i.passed for i in report.invariants if i.name != "payments_match_provider")
    books = "ledger balanced and matches the provider" if ledger_ok else "the ledger is wrong too"
    return f"RESULT: money check FAILED (dashboard {dashboard.status}, {books}; failed: {failed})"


def render(run: LostAnswerRun) -> str:
    events = {e.id: e for e in run.provider.events}
    stuck = run.after_webhooks
    out = [
        "Klaxon week 2: the provider charges a payment, but its answer never reaches us",
        f"REAL RUN on this Mac's Docker Compose stack (seed {run.seed}, run {run.run_id})."
        " Synthetic data, simulated provider, no real money.",
        "The lost answer is injected on purpose. It shows the money check Klaxon will use to verify fixes;"
        " it is not the agent.",
        "",
        f"Payment confirmer: {run.confirmer} ({CONFIRMERS[run.confirmer]})",
        f"  the running container has CONFIRMER_HANDLER={run.container_confirmer}",
        "",
        "Requests, client -> payments API -> fake provider",
    ]
    for n, request in enumerate(run.requests, start=1):
        body = request["body"]
        payment = body.get("payment", body)
        note = (
            "the provider saved the charge, then answered 500 to every retry; the client gives up"
            if n == run.lost_position
            else f"({payment.get('provider_payment_id')})"
        )
        out.append(
            f"  {payment['id']}  {payment['customer']:<8} {format_cents(payment['amount_cents']):>8}"
            f"  -> {request['status']} {payment['status']:<9}  {note}"
        )
    refund = run.refund
    refund_answer = refund["body"].get("status") or refund["body"].get("error", {}).get("code")
    out += [
        "",
        f"After every webhook was handled: {stuck['id']} is {stuck['status']}"
        f" (provider id {stuck['provider_payment_id'] or 'unknown'})",
        f"Refund of half of it ({format_cents(stuck['amount_cents'] // 2)}) -> {refund['status']} {refund_answer}",
        f"Payments API answers to the client: {', '.join(f'{n} x {code}' for code, n in run.api_answers.items())}",
        "",
        "Webhook deliveries, fake provider -> webhook receiver -> Kafka -> ledger and payment confirmer",
    ]
    for n, attempt in enumerate(run.attempts, start=1):
        e = events[attempt["event_id"]]
        out.append(
            f"  #{n:<2} {e.id}  {e.type:<17}  {e.payment_id}  {format_cents(e.amount_cents):>8}  -> {attempt['status']}"
        )
    d = run.dashboard
    out += [
        "",
        "Dashboard, what HTTP monitoring of the webhook endpoint sees",
        f"  requests          {d.requests}",
        f"  2xx / 4xx / 5xx   {d.ok} / {d.client_errors} / {d.server_errors}",
        f"  error rate        {d.error_rate:.1%}",
        f"  status            {d.status}",
        "",
        f"Money check, our books vs the provider's own records (settled after {run.settling.waited_seconds} s)",
    ]
    for inv in run.report.invariants:
        out.append(f"  {'PASS' if inv.passed else 'FAIL'}  {inv.name:<24} {inv.summary}")
    out += ["", verdict(run)]
    return "\n".join(out)


def evidence(run: LostAnswerRun, command: str, now: datetime) -> dict:
    return {
        "label": LABEL,
        "scenario": SCENARIO,
        "confirmer": run.confirmer,
        "confirmer_description": CONFIRMERS[run.confirmer],
        "container_confirmer_handler": run.container_confirmer,
        "seed": run.seed,
        "run_id": run.run_id,
        **provenance(command, now),
        "money_check_passed": run.report.passed,
        "lost_answer": {"position": run.lost_position, "payment_id": run.stuck_payment_id},
        "requests": run.requests,
        "after_webhooks": run.after_webhooks,
        "refund": run.refund,
        "api_answers": run.api_answers,
        "settling": asdict(run.settling),
        "dashboard": asdict(run.dashboard),
        "money_check": run.report.to_dict(),
        "provider": {
            "balance_cents": run.provider.balance_cents(),
            "events": [asdict(e) for e in run.provider.events],
            "payments": [asdict(p) for p in run.provider.payments],
            "delivery_attempts": run.attempts,
        },
        "ledger_transactions": run.ledger_transactions,
        "our_payments": run.our_payments,
        "printed_output": render(run).splitlines(),
    }


def write_evidence(run: LostAnswerRun, directory: Path, command: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(timezone.utc):%Y-%m-%dT%H%M%SZ}_{SCENARIO}_{run.confirmer}_seed{run.seed}"
    path = directory / f"{stem}.json"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}.json"
        n += 1
    path.write_text(json.dumps(evidence(run, command, now), indent=2, default=str) + "\n")
    return path
