"""Demo: the provider refunds a payment, but its answer never reaches us. Is the refund confirmed?

A real run on the live Docker Compose stack (synthetic data, simulated provider).
Three payments, with the first piece's customers and amounts for the seed, and
all three go through. Then the second is refunded in full. For that refund,
the fake provider saves it, queues its webhook, and then answers our payments
API with a 500, to every retry with the same key. The API cannot know whether
the money went back, so it answers the client 503 and leaves the refund
pending. The client gives up. Later, a support agent asks for the same refund
again, as a new request.

- With the payment confirmer (CONFIRMER_HANDLER=by_our_id), the refund's
  webhook confirms it through our refund id in its metadata. It ends
  succeeded, our API refuses the second request itself (nothing is left to
  refund), and all five invariants pass.
- With the known-bad confirmer (by_provider_id), the refund stays pending.
  Our API counts only succeeded refunds, so it sends the second request on to
  the provider, and only the provider's own limit refuses it. The webhook
  endpoint stays green, and the ledger balances and matches the provider,
  because it booked the refund from the webhook. Only the fifth invariant
  fails.

Like the lost-answer demo, it first waits until the stack has settled and the
money check passes. `make demo-lost-refund` starts from a fresh stack.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from klaxon.checks.live import PROVIDER_URL, ProviderAPI, Settling, read_ledger_and_check, settle
from klaxon.checks.money import MoneyReport, format_cents
from klaxon.demo.live import (
    API_URL,
    DemoError,
    ledger_transactions,
    our_payments,
    our_refunds,
    provenance,
    scenario_from_first_piece,
)
from klaxon.demo.lost_answer import clean_start, verdict
from klaxon.patient import stack
from klaxon.patient.dashboard import Dashboard, summarize
from klaxon.patient.ledger import RequestRecord

SCENARIO = "live-lost-refund"
LABEL = (
    "Real run on this Mac's Docker Compose stack: payments API, fake provider, webhook receiver, Kafka,"
    " ledger consumer, payment confirmer, PostgreSQL and Redis. Synthetic data and a simulated provider: no real"
    " money, no real payment company. The lost answer is injected on purpose through the simulator's controls."
    " It shows the money check Klaxon will use to verify fixes; it is not the Klaxon agent."
)
CONFIRMERS = {
    "by_our_id": "finds the refund by our own id, from the metadata the provider sends back",
    "by_provider_id": "the known-bad baseline: finds the refund by the provider's id, which a lost answer never gave us",
}
PAYMENTS, REFUNDED = 3, 2  # three payments; the second is refunded in full


@dataclass
class LostRefundRun:
    confirmer: str
    container_confirmer: str
    seed: int
    run_id: str  # in every idempotency key, so the scenario can run again on the same stack
    stuck_refund_id: str  # our id for the refund whose answer was lost
    requests: list[dict]  # what we sent the payments API, and what it answered: payments, then the refund
    after_webhooks: dict  # the payments API's view of the refunded payment once every webhook was handled
    asked_again: dict  # the support agent's second request for the same refund, and the answer
    settling: Settling
    attempts: list[dict]  # webhook delivery attempts for this scenario's events
    provider: ProviderAPI
    report: MoneyReport
    dashboard: Dashboard
    ledger_transactions: list[dict]
    our_payments: list[dict]
    our_refunds: list[dict]

    @property
    def api_answers(self) -> dict[int, int]:
        """Status codes our payments API gave the client, and how many times each."""
        return dict(sorted(Counter(r["status"] for r in [*self.requests, self.asked_again]).items()))

    @property
    def stuck_refund(self) -> dict:
        """The refund whose answer was lost, as the payments API showed it once every webhook was handled."""
        return next(r for r in self.after_webhooks["refunds"] if r["id"] == self.stuck_refund_id)


def run_live(confirmer: str, seed: int = 7, settle_timeout: float = 60.0) -> LostRefundRun:
    if confirmer not in CONFIRMERS:
        raise DemoError(f"unknown confirmer {confirmer!r}")
    container_confirmer = stack.container_env("payment-confirmer").get("CONFIRMER_HANDLER", "by_our_id")
    if container_confirmer != confirmer:
        raise DemoError(
            f"the running payment confirmer uses {container_confirmer!r}, not {confirmer!r}: make up CONFIRMER={confirmer}"
        )
    customers = scenario_from_first_piece(seed, PAYMENTS).payments
    run_id = uuid.uuid4().hex[:8]

    with (
        httpx.Client(base_url=PROVIDER_URL, timeout=10.0, trust_env=False) as provider,
        httpx.Client(base_url=API_URL, timeout=30.0, trust_env=False) as api,
    ):
        known_events = {e.id for e in clean_start(settle_timeout).events}
        provider.put("/admin/behavior", json={}).raise_for_status()  # nothing else misbehaves in this run

        requests, paid = [], []
        for n, (customer, amount) in enumerate(customers, start=1):
            key = f"lost-refund-{run_id}-payment-{n}"
            response = api.post(
                "/payments", json={"customer": customer, "amount_cents": amount}, headers={"Idempotency-Key": key}
            )
            if response.status_code != 201:
                raise DemoError(f"payment {n}: expected 201, got {response.status_code} {response.text[:300]}")
            requests.append({"call": "POST /payments", "idempotency_key": key, "status": 201, "body": response.json()})
            paid.append(response.json())

        refunded = paid[REFUNDED - 1]
        key = f"lost-refund-{run_id}-refund-1"
        provider.put("/admin/behavior", json={"fail_after_refund_rate": 1.0}).raise_for_status()
        try:
            response = api.post(
                f"/payments/{refunded['id']}/refunds",
                json={"amount_cents": refunded["amount_cents"]},
                headers={"Idempotency-Key": key},
            )
        finally:
            provider.put("/admin/behavior", json={}).raise_for_status()
        if response.status_code != 503:
            raise DemoError(f"the refund: expected 503, got {response.status_code} {response.text[:300]}")
        requests.append(
            {
                "call": f"POST /payments/{refunded['id']}/refunds",
                "idempotency_key": key,
                "status": 503,
                "body": response.json(),
            }
        )
        stuck_refund_id = response.json()["refund"]["id"]

        settling = settle(settle_timeout)
        if not settling.settled:
            raise DemoError(f"the stack did not settle within {settle_timeout} s: {settling}")
        after_webhooks = api.get(f"/payments/{refunded['id']}").raise_for_status().json()

        key = f"lost-refund-{run_id}-refund-2"  # a new request, not a retry: a new key
        response = api.post(
            f"/payments/{refunded['id']}/refunds",
            json={"amount_cents": refunded["amount_cents"]},
            headers={"Idempotency-Key": key},
        )
        if response.status_code != 422:
            raise DemoError(f"the second request: expected 422, got {response.status_code} {response.text[:300]}")
        asked_again = {
            "call": f"POST /payments/{refunded['id']}/refunds",
            "idempotency_key": key,
            "status": 422,
            "body": response.json(),
        }

        settling = settle(settle_timeout)
        if not settling.settled:
            raise DemoError(f"the stack did not settle within {settle_timeout} s after asking again: {settling}")
        attempts = provider.get("/admin/deliveries", params={"attempts": "true"}).raise_for_status().json()["attempts"]

    snapshot, report = read_ledger_and_check(settling.cutoff)
    attempts = [a for a in attempts if a["event_id"] not in known_events]
    return LostRefundRun(
        confirmer=confirmer,
        container_confirmer=container_confirmer,
        seed=seed,
        run_id=run_id,
        stuck_refund_id=stuck_refund_id,
        requests=requests,
        after_webhooks=after_webhooks,
        asked_again=asked_again,
        settling=settling,
        attempts=attempts,
        provider=snapshot,
        report=report,
        # What HTTP monitoring of our webhook endpoint would see: every status code it answered in this scenario.
        dashboard=summarize(
            RequestRecord(a["event_id"], a["status"], "answered") for a in attempts if a["status"] is not None
        ),
        ledger_transactions=ledger_transactions(),
        our_payments=our_payments(),
        our_refunds=our_refunds(),
    )


def render(run: LostRefundRun) -> str:
    events = {e.id: e for e in run.provider.events}
    out = [
        "Klaxon week 2: the provider refunds a payment, but its answer never reaches us",
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
    for request in run.requests:
        body = request["body"]
        if "refund" in body:
            refund = body["refund"]
            out.append(
                f"  {refund['id']}  refund of {refund['payment_id']} {format_cents(refund['amount_cents']):>8}"
                f"  -> {request['status']} {refund['status']:<9}  the provider saved the refund, then answered 500"
                " to every retry; the client gives up"
            )
        else:
            out.append(
                f"  {body['id']}  {body['customer']:<8} {format_cents(body['amount_cents']):>8}"
                f"  -> {request['status']} {body['status']:<9}  ({body['provider_payment_id']})"
            )
    stuck, again = run.stuck_refund, run.asked_again["body"]
    out += [
        "",
        f"After every webhook was handled: {stuck['id']} is {stuck['status']}"
        f" (provider id {stuck['provider_refund_id'] or 'unknown'})",
        f"Asked again by support, as a new request ({again['refund']['id']}, in full)"
        f" -> {run.asked_again['status']} {again['error']['code']}: {again['error']['message']}",
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


def evidence(run: LostRefundRun, command: str, now: datetime) -> dict:
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
        "lost_answer": {"refund_id": run.stuck_refund_id, "of_payment": REFUNDED},
        "requests": run.requests,
        "after_webhooks": run.after_webhooks,
        "asked_again": run.asked_again,
        "api_answers": run.api_answers,
        "settling": asdict(run.settling),
        "dashboard": asdict(run.dashboard),
        "money_check": run.report.to_dict(),
        "provider": {
            "balance_cents": run.provider.balance_cents(),
            "events": [asdict(e) for e in run.provider.events],
            "payments": [asdict(p) for p in run.provider.payments],
            "refunds": [asdict(r) for r in run.provider.refunds],
            "delivery_attempts": run.attempts,
        },
        "ledger_transactions": run.ledger_transactions,
        "our_payments": run.our_payments,
        "our_refunds": run.our_refunds,
        "printed_output": render(run).splitlines(),
    }


def write_evidence(run: LostRefundRun, directory: Path, command: str, now: datetime | None = None) -> Path:
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
