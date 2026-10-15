"""Demo: one fault from klaxon.faults, injected on purpose into the live stack while traffic flows.

A real run on this Mac's Docker Compose stack (synthetic data, simulated provider). It starts the traffic
generator, requires a passing money check, injects the fault, and watches: container health, the metrics
Klaxon will read (from Prometheus, as the dashboard does), and the money check. Then it clears the fault's
cause, watches again, stops the traffic and checks once more. What it saw is the evidence of what the
fault looks like: what Klaxon will have to explain from the outside. It is not the Klaxon agent.

The money check runs as Klaxon will run it. When it cannot settle (a stalled consumer), it says so, and
the check is run once more without waiting, to show what the money looks like meanwhile; events still in
flight then count as lost too, so that view is an upper bound. Each fault says what the money check
should show in each phase; the run passes (exit 0) when it did.
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from klaxon import faults, traffic
from klaxon.checks.live import LiveCheck, NotSettled, check_live
from klaxon.checks.money import format_cents
from klaxon.demo.live import DemoError, provenance
from klaxon.patient import stack

SCENARIO = "live-fault"
PROMETHEUS_URL = "http://127.0.0.1:9090"
LABEL = (
    "Real run on this Mac's Docker Compose stack, with synthetic traffic from klaxon.traffic and one fault from"
    " klaxon.faults injected on purpose (synthetic data, a simulated provider, no real money). It records what the"
    " fault looks like from the outside; it is not the Klaxon agent."
)
# What the run samples, as the dashboard's panels do (see docker/grafana/klaxon-patient.json).
SIGNALS = {
    "webhooks_refused_per_s": 'sum(rate(klaxon_webhooks_received_total{result!="queued"}[1m]))',
    "consumer_lag": "sum by (consumer, partition) (klaxon_kafka_consumer_lag)",
    "consumer_retries_per_s": "sum by (consumer) (rate(klaxon_consumer_retries_total[1m]))",
    "dead_letters": "sum by (consumer, reason) (klaxon_consumer_dead_letters_total)",
    "ledger_duplicates_skipped_per_s": (
        'sum(rate(klaxon_consumer_events_total{consumer="ledger", outcome="already_processed"}[1m]))'
    ),
    "api_5xx_per_s": (
        'sum(rate(http_server_request_duration_seconds_count{job="klaxon-patient/payments-api",'
        ' http_route!="/healthz", http_response_status_code=~"5.."}[1m]))'
    ),
    "api_p95_seconds": (
        "histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket"
        '{job="klaxon-patient/payments-api", http_route="/payments"}[1m])))'
    ),
    "provider_no_answer_per_s": 'sum(rate(klaxon_provider_attempts_total{result="no_answer"}[1m]))',
    "payments_pending": "sum by (age) (klaxon_payments_pending)",
}


@dataclass
class Moment:
    phase: str  # before, during, after
    seconds: float  # since the fault was injected (negative: before it)
    at_utc: str
    unhealthy: list[str]  # containers that are not healthy (one-shot ones that exited are left out)
    signals: dict[str, list[dict]]  # name -> [{"labels": {...}, "value": float or None}]

    def total(self, name: str, **labels: str) -> float | None:
        values = [
            point["value"]
            for point in self.signals.get(name, [])
            if point["value"] is not None and all(point["labels"].get(k) == v for k, v in labels.items())
        ]
        return sum(values) if values else None


@dataclass
class MoneyView:
    phase: str  # before, during, after, quiet
    outcome: str  # passed, failed, could not run
    findings: list[str]  # each failed invariant, or why the check could not run
    problems: dict[str, dict[str, int]] = field(default_factory=dict)  # invariant -> problem -> how many
    money_off_cents: int | None = None
    without_waiting: MoneyView | None = None  # when it could not settle: the same check without waiting


@dataclass
class FaultRun:
    fault: dict
    settings: traffic.Settings
    timings: dict[str, float]
    injected: dict
    cleared: dict
    moments: list[Moment]
    money: list[MoneyView]
    traffic: dict
    behavior_note: str

    def surprises(self) -> list[str]:
        """Each phase where the money check said something other than what the fault expects."""
        said = {view.phase: view.outcome for view in self.money}
        return [
            f"{phase}: expected {expected}, got {said.get(phase, 'nothing')}"
            for phase, expected in self.fault["expected_money"].items()
            if said.get(phase) != expected
        ]

    @property
    def as_expected(self) -> bool:
        return not self.surprises()


def prometheus(query: str, http: httpx.Client) -> list[dict]:
    response = http.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
    response.raise_for_status()
    out = []
    for item in response.json()["data"]["result"]:
        value = float(item["value"][1])
        out.append({"labels": item["metric"], "value": None if math.isnan(value) or math.isinf(value) else value})
    return out


def unhealthy_containers() -> list[str]:
    """Every service whose container is not running and healthy, except the one-shot topic creator."""
    text = stack.compose("ps", "--all", "--format", "json").strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
    out = []
    for row in rows:
        service, state, health = row.get("Service"), row.get("State"), row.get("Health") or ""
        if service == "kafka-init":
            continue
        if state != "running" or health not in ("healthy", ""):
            out.append(f"{service} ({health or state})")
    return sorted(out)


def sample(phase: str, seconds: float, http: httpx.Client) -> Moment:
    signals = {}
    for name, query in SIGNALS.items():
        try:
            signals[name] = prometheus(query, http)
        except httpx.HTTPError:
            signals[name] = []
    return Moment(
        phase,
        round(seconds, 1),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        unhealthy_containers(),
        signals,
    )


def _kind(item: dict) -> str:
    """What one evidence item of an invariant says, in a few words, for counting."""
    if "problem" in item:
        return str(item["problem"])
    if item.get("scope") in ("provider_clearing, all payments", "whole ledger"):
        return "totals differ"
    if "scope" in item:
        return "payments that differ"
    return "unbalanced transactions"


def money_view(phase: str, settle_timeout: float = 20.0, wait: bool = True) -> MoneyView:
    try:
        result: LiveCheck = check_live(wait=wait, timeout_seconds=settle_timeout)
    except NotSettled as exc:
        view = MoneyView(phase, "could not run", [str(exc)])
        view.without_waiting = money_view(phase, wait=False)
        return view
    except Exception as exc:  # unreachable: no verdict either way
        return MoneyView(phase, "could not run", [repr(exc)[:300]])
    report = result.report
    failed = [i for i in report.invariants if not i.passed]
    return MoneyView(
        phase,
        "passed" if report.passed else "failed",
        [f"{i.name}: {i.summary[:300]}" for i in failed],
        {i.name: dict(Counter(_kind(item) for item in i.evidence)) for i in failed},
        report.money_off_cents,
    )


def watch(phase: str, start: float, seconds: float, every: float, http: httpx.Client, moments: list[Moment]) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(every, max(0.0, deadline - time.monotonic())))
        moments.append(sample(phase, time.monotonic() - start, http))
        print(f"  {phase}: {_summary_line(moments[-1], moments[0])}", file=sys.stderr, flush=True)


def run_live(
    name: str,
    rate: float = 3.0,
    seed: int = 7,
    warmup_seconds: float = 10.0,
    during_seconds: float = 60.0,
    after_seconds: float = 60.0,
    every_seconds: float = 10.0,
) -> FaultRun:
    fault = faults.get(name)
    local = faults.LocalStack()
    http = httpx.Client(timeout=10.0, trust_env=False)
    settings = traffic.Settings(rate=rate, seed=seed)
    generator = traffic.TrafficGenerator(settings)
    moments: list[Moment] = []
    money: list[MoneyView] = []
    injected: dict = {}
    cleared: dict = {}
    print(f"Traffic on ({rate:g} payments per second); warming up {warmup_seconds:g} s", file=sys.stderr, flush=True)
    generator.start()
    try:
        time.sleep(warmup_seconds)
        money.append(money_view("before"))
        if money[-1].outcome != "passed":
            raise DemoError(
                f"the money check does not pass before the fault ({money[-1].findings}): make reset, make up"
            )
        moments.append(sample("before", 0.0, http))
        print(f"Injecting {fault.name}", file=sys.stderr, flush=True)
        start = time.monotonic()
        try:
            injected = fault.inject(local)
            injected_at = time.monotonic() - start  # a redeploy takes a few seconds
            watch("during", start, during_seconds, every_seconds, http, moments)
            money.append(money_view("during"))
        finally:
            print(f"Clearing {fault.name}", file=sys.stderr, flush=True)
            cleared = fault.clear(local)  # whatever happened, never leave the stack with the fault's cause in it
        cleared_at = time.monotonic() - start
        watch("after", start, after_seconds, every_seconds, http, moments)
        money.append(money_view("after", settle_timeout=60.0))
    finally:
        generator.close()
        generator.join()
    money.append(money_view("quiet", settle_timeout=60.0))
    return FaultRun(
        fault=fault.describe(),
        settings=settings,
        timings={
            "warmup_seconds": warmup_seconds,
            "during_seconds": during_seconds,
            "after_seconds": after_seconds,
            "injected_at_seconds": round(injected_at, 1),
            "cleared_at_seconds": round(cleared_at, 1),
            "every_seconds": every_seconds,
        },
        injected=injected,
        cleared=cleared,
        moments=moments,
        money=money,
        traffic=generator.stats.to_dict(),
        behavior_note=generator.behavior_note,
    )


def _number(value: float | None, unit: str = "", digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}{unit}"


def _since(m: Moment, base: Moment | None, name: str, **labels: str) -> float | None:
    """A counter's change since the first sample: totals count every run since the stack started."""
    now = m.total(name, **labels)
    if now is None or base is None:
        return now
    return now - (base.total(name, **labels) or 0.0)


def _summary_line(m: Moment, base: Moment | None = None) -> str:
    return (
        f"t={m.seconds:>5.0f} s  unhealthy: {', '.join(m.unhealthy) or 'none'};"
        f" ledger lag {_number(m.total('consumer_lag', consumer='ledger'), digits=0)},"
        f" confirmer lag {_number(m.total('consumer_lag', consumer='payment-confirmer'), digits=0)};"
        f" retries/s {_number(m.total('consumer_retries_per_s'))};"
        f" new dead letters: ledger {_number(_since(m, base, 'dead_letters', consumer='ledger'), digits=0)},"
        f" confirmer {_number(_since(m, base, 'dead_letters', consumer='payment-confirmer'), digits=0)};"
        f" ledger duplicates skipped/s {_number(m.total('ledger_duplicates_skipped_per_s'))};"
        f" API 5xx/s {_number(m.total('api_5xx_per_s'))}, p95 {_number(m.total('api_p95_seconds'), ' s')};"
        f" provider no answer/s {_number(m.total('provider_no_answer_per_s'))};"
        f" pending over 1 min {_number(m.total('payments_pending', age='over_1m'), digits=0)}"
    )


def _money_line(view: MoneyView, indent: str = "  ") -> list[str]:
    head = f"{indent}{view.phase:<7} {view.outcome.upper()}"
    if view.money_off_cents:
        head += f" (books off by {format_cents(view.money_off_cents, signed=True)})"
    lines = [head, *(f"{indent}        {finding[:220]}" for finding in view.findings)]
    for invariant, problems in view.problems.items():
        lines.append(f"{indent}        {invariant}: " + ", ".join(f"{n} {problem}" for problem, n in problems.items()))
    if view.without_waiting is not None:
        lines.append(f"{indent}        the same check without waiting (events still in flight count as lost too):")
        lines += _money_line(view.without_waiting, indent + "        ")
    return lines


def render(run: FaultRun) -> str:
    f, s = run.fault, run.settings
    out = [
        f"Klaxon week 2: a fault injected on purpose: {f['name']}",
        f"REAL RUN on this Mac's Docker Compose stack, with synthetic traffic (run {s.run_id}, seed {s.seed},"
        f" {s.rate:g} payments per second). Synthetic data, simulated provider, no real money. Not the agent.",
        "",
        f"The fault: {f['title']}",
        f"  cause: {f['cause']}",
        *(f"  expected: {symptom}" for symptom in f["expected_symptoms"]),
        f"  injected (took {run.timings['injected_at_seconds']:g} s): {json.dumps(run.injected)}",
        f"  cleared after {run.timings['cleared_at_seconds']:g} s: {json.dumps(run.cleared)}",
        "",
        "What the stack showed (Prometheus and container health, sampled; t counts from the start of the injection)",
        "  Rates are over the last minute, so they trail a change by up to a minute.",
        *(f"  {m.phase:<6} {_summary_line(m, run.moments[0] if run.moments else None)}" for m in run.moments),
        "",
        "Money check (expected: " + ", ".join(f"{k} {v}" for k, v in f["expected_money"].items()) + ")",
        *(line for view in run.money for line in _money_line(view)),
        "",
        "The traffic generator",
        *traffic.summary(run.traffic["counts"]),
        f"  {run.behavior_note}",
        "",
        "RESULT: the money check said what the fault expects in every phase"
        if run.as_expected
        else "RESULT: NOT as expected: " + "; ".join(run.surprises()),
    ]
    return "\n".join(out)


def evidence(run: FaultRun, command: str, now: datetime) -> dict:
    return {
        "label": LABEL,
        "scenario": SCENARIO,
        "fault": run.fault,
        **provenance(command, now),
        "traffic_settings": asdict(run.settings),
        "timings": run.timings,
        "injected": run.injected,
        "cleared": run.cleared,
        "moments": [asdict(m) for m in run.moments],
        "money_checks": [asdict(v) for v in run.money],
        "as_expected": run.as_expected,
        "surprises": run.surprises(),
        "traffic": run.traffic,
        "provider_behavior": run.behavior_note,
        "printed_output": render(run).splitlines(),
    }


def write_evidence(run: FaultRun, directory: Path, command: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(timezone.utc):%Y-%m-%dT%H%M%SZ}_{SCENARIO}_{run.fault['name']}_seed{run.settings.seed}"
    path = directory / f"{stem}.json"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}.json"
        n += 1
    path.write_text(json.dumps(evidence(run, command, now), indent=2, default=str) + "\n")
    return path
