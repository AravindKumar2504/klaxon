"""Demo: the provider delivers one webhook twice. Does anything notice?

Synthetic and seeded: in-process, no network, no real money, no real provider.
It shows the check Klaxon will use to verify fixes; it is not the agent.
"""

from __future__ import annotations

import hashlib
import json
import platform
import random
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import klaxon
from klaxon.checks.money import MoneyReport, check_money, format_cents
from klaxon.patient.dashboard import Dashboard, summarize
from klaxon.patient.ledger import Ledger, RequestRecord, WebhookEndpoint
from klaxon.patient.provider import Delivery, FakeProvider

SCENARIO = "duplicate-webhook"
LABEL = (
    "Synthetic, seeded demo. In-process simulation: no network, no real money, no real provider."
    " It shows the money check Klaxon will use to verify fixes; it is not the Klaxon agent."
)
DEMO_SECRET = b"klaxon-demo-secret-not-real"
HANDLERS = {
    "naive": "posts every delivery it receives",
    "idempotent": "dedupes by event id with a unique constraint",
}


@dataclass
class RunResult:
    handler: str
    seed: int
    provider: FakeProvider
    ledger: Ledger
    deliveries: list[Delivery]
    requests: list[RequestRecord]
    dashboard: Dashboard
    report: MoneyReport


def build_provider(seed: int, payments: int = 5) -> FakeProvider:
    """A few seeded payments and one partial refund."""
    if payments < 1:
        raise ValueError("need at least one payment")
    rng = random.Random(seed)
    provider = FakeProvider(DEMO_SECRET, seed=seed)
    for i in range(1, payments + 1):
        provider.create_payment(f"cus_{i:03d}", rng.randrange(500, 12_001))  # $5.00 to $120.00
    refunded = provider.payments[rng.choice(sorted(provider.payments))]
    provider.refund(refunded.id, refunded.amount_cents // 2)
    return provider


def run(handler: str, seed: int = 7, payments: int = 5) -> RunResult:
    provider = build_provider(seed, payments)
    ledger = Ledger()
    endpoint = WebhookEndpoint(ledger, DEMO_SECRET, handler, now=provider.clock.now)
    deliveries = provider.deliver(endpoint, provider.plan("duplicate"))
    return RunResult(
        handler=handler,
        seed=seed,
        provider=provider,
        ledger=ledger,
        deliveries=deliveries,
        requests=endpoint.requests,
        dashboard=summarize(endpoint.requests),
        report=check_money(provider, ledger.conn),
    )


def verdict(result: RunResult) -> str:
    report, dashboard = result.report, result.dashboard
    balanced = "ledger balanced" if report.invariant("ledger_balanced").passed else "ledger NOT balanced"
    if report.passed:
        return f"RESULT: money check PASSED (dashboard {dashboard.status}, {balanced}, ledger matches the provider)"
    if report.money_off_cents:
        detail = f"money off by {format_cents(report.money_off_cents, signed=True)}"
    else:
        failed = ", ".join(i.name for i in report.invariants if not i.passed)
        detail = f"totals agree, but failed: {failed}"
    return f"RESULT: money check FAILED (dashboard {dashboard.status}, {balanced}, {detail})"


def render(result: RunResult) -> str:
    dashboard, report = result.dashboard, result.report
    out = [
        "Klaxon first piece: one webhook delivered twice",
        f"SYNTHETIC, SEEDED DEMO (seed {result.seed}). In-process simulation: no network, no real money.",
        "It shows the money check Klaxon will use to verify fixes. It is not the agent.",
        "",
        f"Handler: {result.handler} ({HANDLERS[result.handler]})",
        "",
        "Deliveries, fake provider -> our webhook endpoint",
    ]
    for delivery, request in zip(result.deliveries, result.requests, strict=True):
        e = result.provider.event(delivery.event_id)
        line = (
            f"  #{delivery.n:<2} {e.id}  {e.type:<17}  {e.payment_id}  {format_cents(e.amount_cents):>8}"
            f"  -> {delivery.status} {request.outcome:<17}  {delivery.note}"
        )
        out.append(line.rstrip())
    out += [
        "",
        "Dashboard, what HTTP monitoring sees",
        f"  requests          {dashboard.requests}",
        f"  2xx / 4xx / 5xx   {dashboard.ok} / {dashboard.client_errors} / {dashboard.server_errors}",
        f"  error rate        {dashboard.error_rate:.1%}",
        f"  status            {dashboard.status}",
        "",
        "Money check, ledger vs the provider's own records",
    ]
    for inv in report.invariants:
        out.append(f"  {'PASS' if inv.passed else 'FAIL'}  {inv.name:<20} {inv.summary}")
    out += ["", verdict(result)]
    return "\n".join(out)


def code_fingerprint() -> str:
    """SHA-256 over the package source, so evidence is tied to exact code even before a commit."""
    root = Path(klaxon.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], cwd=Path(klaxon.__file__).parent, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def evidence(result: RunResult, command: str, now: datetime) -> dict:
    status = _git("status", "--porcelain", "--", ".")
    return {
        "label": LABEL,
        "scenario": SCENARIO,
        "handler": result.handler,
        "handler_description": HANDLERS[result.handler],
        "seed": result.seed,
        "rerun_command": command,  # rebuilt from the arguments, run from the repo root in its .venv
        "ran_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "ran_at_local": now.astimezone().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "klaxon_version": klaxon.__version__,
        "code_sha256": code_fingerprint(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_source_clean": None if status is None else status == "",
        "money_check_passed": result.report.passed,
        "dashboard": asdict(result.dashboard),
        "money_check": result.report.to_dict(),
        "provider": {
            "note": "simulated clock; timestamps are not real",
            "balance_cents": result.provider.balance_cents(),
            "payments": [asdict(p) for p in result.provider.payments.values()],
            "events": [asdict(e) for e in result.provider.events],
        },
        "deliveries": [
            asdict(d) | {"outcome": r.outcome} for d, r in zip(result.deliveries, result.requests, strict=True)
        ],
        "ledger_transactions": result.ledger.transactions(),
        "printed_output": render(result).splitlines(),
    }


def write_evidence(result: RunResult, directory: Path, command: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(timezone.utc):%Y-%m-%dT%H%M%SZ}_{SCENARIO}_{result.handler}_seed{result.seed}"
    path = directory / f"{stem}.json"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}.json"
        n += 1
    path.write_text(json.dumps(evidence(result, command, now), indent=2) + "\n")
    return path
