"""Demo: the first piece's scenario, replayed through the live stack.

Same seed, same five payments and one refund, and the same lost
acknowledgement as `python -m klaxon.demo duplicate-webhook`, but this time
through real software running in Docker Compose: the payments API, the fake
provider's webhook sender, the webhook receiver, Kafka, the ledger consumer,
PostgreSQL and Redis. The data is synthetic and the provider is a simulator.

It needs a fresh stack (`make demo-live` resets it), so the live event ids
line up with the first piece's: evt_000003 is the third event, as evt_0003 was.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg

import klaxon
from klaxon.checks.live import (
    PROVIDER_URL,
    READONLY_DSN,
    ProviderAPI,
    Settling,
    read_ledger_and_check,
    wait_until_settled,
)
from klaxon.checks.money import MoneyReport, format_cents
from klaxon.demo.duplicate_webhook import build_provider, code_fingerprint
from klaxon.patient import stack
from klaxon.patient.dashboard import Dashboard, summarize
from klaxon.patient.ledger import RequestRecord
from klaxon.patient.provider import REFUND_SUCCEEDED

SCENARIO = "live-duplicate-webhook"
API_URL = "http://127.0.0.1:8080"
LABEL = (
    "Real run on this Mac's Docker Compose stack: payments API, fake provider, webhook receiver, Kafka,"
    " ledger consumer, PostgreSQL and Redis. Synthetic data and a simulated provider: no real money, no real"
    " payment company. It shows the money check Klaxon will use to verify fixes; it is not the Klaxon agent."
)
HANDLERS = {
    "naive": "posts every delivery it receives",
    "idempotent": "dedupes by event id with a unique constraint, in the same transaction as the posting",
}


class DemoError(RuntimeError):
    """The demo could not run as designed. Nothing it printed should be read as a result."""


@dataclass(frozen=True)
class Scenario:
    """The first piece's story for one seed, read from the first piece's own code so the two cannot drift."""

    seed: int
    payments: list[tuple[str, int]]  # (customer, amount_cents), in creation order
    refund_of: int  # which payment is refunded, 1-based
    refund_cents: int
    lost_ack_position: int  # which event's first acknowledgement is lost, 1-based

    @property
    def lost_ack_event_id(self) -> str:
        return f"evt_{self.lost_ack_position:06d}"  # the live provider numbers events in creation order


def scenario_from_first_piece(seed: int, payments: int = 5) -> Scenario:
    provider = build_provider(seed, payments)
    retried = provider.plan("duplicate")[-1].event  # the duplicate schedule appends the retry last
    refund = next(e for e in provider.events if e.type == REFUND_SUCCEEDED)
    return Scenario(
        seed=seed,
        payments=[(p.customer, p.amount_cents) for p in provider.payments.values()],
        refund_of=list(provider.payments).index(refund.payment_id) + 1,
        refund_cents=refund.amount_cents,
        lost_ack_position=provider.events.index(retried) + 1,
    )


@dataclass
class LiveRun:
    handler: str
    scenario: Scenario
    container_handler: str
    requests: list[dict]  # what we sent the payments API, and what it answered
    settling: Settling
    delivery_stats: dict
    attempts: list[dict]
    provider: ProviderAPI
    report: MoneyReport
    dashboard: Dashboard
    ledger_transactions: list[dict]


def _readonly_rows(query: str) -> list[tuple]:
    with psycopg.connect(READONLY_DSN, connect_timeout=5) as conn:
        return conn.execute(query).fetchall()


def ensure_fresh(provider: httpx.Client) -> None:
    events = provider.get("/v1/events", params={"limit": 1}).raise_for_status().json()["data"]
    ((transactions,),) = _readonly_rows("SELECT count(*) FROM transactions")
    if events or transactions:
        raise DemoError(
            "this demo needs a fresh stack (event ids must start at evt_000001): run make demo-live, which resets it"
        )


def run_live(handler: str, seed: int = 7, payments: int = 5, settle_timeout: float = 60.0) -> LiveRun:
    if handler not in HANDLERS:
        raise DemoError(f"unknown handler {handler!r}")
    container_handler = stack.container_env("ledger-consumer").get("LEDGER_HANDLER", "idempotent")
    if container_handler != handler:
        raise DemoError(
            f"the running ledger consumer uses {container_handler!r}, not {handler!r}: make up HANDLER={handler}"
        )
    scenario = scenario_from_first_piece(seed, payments)

    with (
        httpx.Client(base_url=PROVIDER_URL, timeout=10.0) as provider,
        httpx.Client(base_url=API_URL, timeout=20.0) as api,
    ):
        ensure_fresh(provider)
        provider.put("/admin/behavior", json={"lose_ack_event_ids": [scenario.lost_ack_event_id]}).raise_for_status()

        requests = []

        def call(path: str, body: dict, key: str) -> dict:
            response = api.post(path, json=body, headers={"Idempotency-Key": key})
            record = {"call": f"POST {path}", "idempotency_key": key, "status": response.status_code}
            if response.status_code != 201:
                raise DemoError(f"{record['call']} was refused: {response.status_code} {response.text[:300]}")
            requests.append(record | {"body": response.json()})
            return requests[-1]["body"]

        payment_ids = [
            call("/payments", {"customer": customer, "amount_cents": amount}, f"demo-seed{seed}-payment-{n}")["id"]
            for n, (customer, amount) in enumerate(scenario.payments, start=1)
        ]
        refunded = payment_ids[scenario.refund_of - 1]
        call(f"/payments/{refunded}/refunds", {"amount_cents": scenario.refund_cents}, f"demo-seed{seed}-refund-1")

        settling = wait_until_settled(settle_timeout)
        if not settling.settled:
            raise DemoError(f"the stack did not settle within {settle_timeout} s: {settling}")
        deliveries = provider.get("/admin/deliveries", params={"attempts": "true"}).raise_for_status().json()

    if deliveries["stats"]["duplicate_deliveries"] < 1:
        raise DemoError("the provider delivered no duplicate, so this run does not show the scenario")
    snapshot = ProviderAPI(PROVIDER_URL)
    return LiveRun(
        handler=handler,
        scenario=scenario,
        container_handler=container_handler,
        requests=requests,
        settling=settling,
        delivery_stats=deliveries["stats"],
        attempts=deliveries["attempts"],
        provider=snapshot,
        report=read_ledger_and_check(snapshot),
        # What HTTP monitoring of our webhook endpoint would see: every status code it answered.
        dashboard=summarize(
            RequestRecord(a["event_id"], a["status"], "answered")
            for a in deliveries["attempts"]
            if a["status"] is not None
        ),
        ledger_transactions=[
            {"id": tx, "event_id": event, "payment_id": payment, "source": source}
            for tx, event, payment, source in _readonly_rows(
                "SELECT id, event_id, payment_id, source FROM transactions ORDER BY id"
            )
        ],
    )


def verdict(run: LiveRun) -> str:
    report, dashboard = run.report, run.dashboard
    balanced = "ledger balanced" if report.invariant("ledger_balanced").passed else "ledger NOT balanced"
    if report.passed:
        return f"RESULT: money check PASSED (dashboard {dashboard.status}, {balanced}, ledger matches the provider)"
    if report.money_off_cents:
        detail = f"money off by {format_cents(report.money_off_cents, signed=True)}"
    else:
        detail = "totals agree, but failed: " + ", ".join(i.name for i in report.invariants if not i.passed)
    return f"RESULT: money check FAILED (dashboard {dashboard.status}, {balanced}, {detail})"


def render(run: LiveRun) -> str:
    events = {e.id: e for e in run.provider.events}
    out = [
        "Klaxon week 1: the first piece's scenario, through the live stack",
        f"REAL RUN on this Mac's Docker Compose stack (seed {run.scenario.seed})."
        " Synthetic data, simulated provider, no real money.",
        "Real software in between: payments API, webhook receiver, Kafka, ledger consumer, PostgreSQL, Redis.",
        "It shows the money check Klaxon will use to verify fixes. It is not the agent.",
        "",
        f"Ledger consumer: {run.handler} ({HANDLERS[run.handler]})",
        f"  the running container has LEDGER_HANDLER={run.container_handler}",
        "",
        "Requests, client -> payments API -> fake provider",
    ]
    for request in run.requests:
        body = request["body"]
        target = f"refund of {body.get('payment_id')}" if "refunds" in request["call"] else body.get("customer")
        provider_id = body.get("provider_payment_id") or body.get("provider_refund_id")
        out.append(
            f"  {body.get('id'):<11} {target:<20} {format_cents(body.get('amount_cents', 0)):>8}"
            f"  -> {request['status']} {body.get('status')}  ({provider_id})"
        )
    out += ["", "Webhook deliveries, fake provider -> webhook receiver -> Kafka"]
    for n, attempt in enumerate(run.attempts, start=1):
        e = events[attempt["event_id"]]
        note = attempt["error"] or ("" if attempt["attempt"] == 1 else f"retry, attempt {attempt['attempt']}")
        out.append(
            f"  #{n:<2} {e.id}  {e.type:<17}  {e.payment_id}  {format_cents(e.amount_cents):>8}"
            f"  -> {attempt['status']}  {note}".rstrip()
        )
    out += ["", "Ledger transactions, as the read-only role sees them (source = Kafka topic/partition@offset)"]
    for tx in run.ledger_transactions:
        out.append(f"  #{tx['id']:<2} {tx['event_id']}  {tx['payment_id']}  {tx['source']}")
    d = run.dashboard
    out += [
        "",
        "Dashboard, what HTTP monitoring sees (the status codes our webhook endpoint answered)",
        f"  requests          {d.requests}",
        f"  2xx / 4xx / 5xx   {d.ok} / {d.client_errors} / {d.server_errors}",
        f"  error rate        {d.error_rate:.1%}",
        f"  status            {d.status}",
        "",
        f"Money check, ledger vs the provider's own records (settled after {run.settling.waited_seconds} s)",
    ]
    for inv in run.report.invariants:
        out.append(f"  {'PASS' if inv.passed else 'FAIL'}  {inv.name:<20} {inv.summary}")
    out += ["", verdict(run)]
    return "\n".join(out)


def stack_fingerprint() -> str:
    """SHA-256 over the files that define the stack, next to code_fingerprint() for the Python."""
    digest = hashlib.sha256()
    files = [stack.REPO / "compose.yaml", stack.REPO / "Makefile"]
    files += sorted(p for folder in ("docker", "requirements") for p in (stack.REPO / folder).rglob("*") if p.is_file())
    for path in files:
        digest.update(path.relative_to(stack.REPO).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def evidence(run: LiveRun, command: str, now: datetime) -> dict:
    def git(*args: str) -> str | None:
        done = subprocess.run(["git", *args], cwd=stack.REPO, capture_output=True, text=True, timeout=5)
        return done.stdout.strip() if done.returncode == 0 else None

    status = git("status", "--porcelain", "--", ".", ":!evidence")
    return {
        "label": LABEL,
        "scenario": SCENARIO,
        "handler": run.handler,
        "handler_description": HANDLERS[run.handler],
        "container_ledger_handler": run.container_handler,
        "seed": run.scenario.seed,
        "rerun_command": command,
        "ran_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "ran_at_local": now.astimezone().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "klaxon_version": klaxon.__version__,
        "code_sha256": code_fingerprint(),
        "stack_files_sha256": stack_fingerprint(),
        "git_commit": git("rev-parse", "HEAD"),
        "git_clean_outside_evidence": None if status is None else status == "",
        "stack": stack.describe(),
        "money_check_passed": run.report.passed,
        "scenario_from_first_piece": asdict(run.scenario) | {"lost_ack_event_id": run.scenario.lost_ack_event_id},
        "requests": run.requests,
        "settling": asdict(run.settling),
        "dashboard": asdict(run.dashboard),
        "money_check": run.report.to_dict(),
        "provider": {
            "balance_cents": run.provider.balance_cents(),
            "events": [asdict(e) for e in run.provider.events],
            "delivery_stats": run.delivery_stats,
            "delivery_attempts": run.attempts,
        },
        "ledger_transactions": run.ledger_transactions,
        "printed_output": render(run).splitlines(),
    }


def write_evidence(run: LiveRun, directory: Path, command: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(timezone.utc):%Y-%m-%dT%H%M%SZ}_{SCENARIO}_{run.handler}_seed{run.scenario.seed}"
    path = directory / f"{stem}.json"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}.json"
        n += 1
    path.write_text(json.dumps(evidence(run, command, now), indent=2, default=str) + "\n")
    return path
