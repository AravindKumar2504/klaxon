"""Demo: the money check, run again and again while synthetic traffic keeps flowing. Does it stay right?

A real run on this Mac's Docker Compose stack. It starts the traffic generator (klaxon.traffic),
lets it run a few seconds, then runs the live money check --checks times, back to back, while the
traffic keeps flowing. Then it stops the traffic and runs one more check on the quiet stack.

On a healthy stack every check must pass. A check that fails under traffic while the quiet check
passes is a false alarm: the check mistook money in flight for money gone wrong (known gap 3).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from klaxon import traffic
from klaxon.checks.live import NotSettled, check_live
from klaxon.demo.live import DemoError, provenance

SCENARIO = "live-check-under-traffic"
LABEL = (
    "Real run on this Mac's Docker Compose stack, with synthetic traffic from klaxon.traffic (made-up customers"
    " and amounts, a simulated provider, no real money). It measures the money check Klaxon will use to verify"
    " fixes, run while traffic flows; it is not the Klaxon agent."
)


@dataclass
class CheckRun:
    n: int  # 0 is the last check, on the quiet stack
    started_utc: str
    seconds: float
    outcome: str  # passed, failed, or could not run
    failed: list[dict]  # each failed invariant: its name, its summary and its first evidence items
    error: str | None
    details: dict | None  # what the check reported about its cutoff and settling
    left_for_next_check: dict[str, int] | None = None  # records newer than the cutoff: proof traffic was in flight


@dataclass
class UnderTrafficRun:
    settings: traffic.Settings
    warmup_seconds: float
    checks: list[CheckRun]
    quiet: CheckRun
    traffic: dict
    behavior_note: str

    def counted(self) -> dict[str, int]:
        return {o: sum(c.outcome == o for c in self.checks) for o in ("passed", "failed", "could not run")}

    @property
    def passed(self) -> bool:
        return all(c.outcome == "passed" for c in [*self.checks, self.quiet])


def one_check(n: int, settle_timeout: float) -> CheckRun:
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    error, failed, details, left = None, [], None, None
    try:
        result = check_live(timeout_seconds=settle_timeout)
    except NotSettled as exc:
        outcome, error = "could not run", str(exc)
    except Exception as exc:  # the stack could not be reached: no result, so not a failure either
        outcome, error = "could not run", repr(exc)[:300]
    else:
        report = result.to_dict()
        details = {key: value for key, value in report.items() if key != "money_check"}
        outcome = "passed" if result.report.passed else "failed"
        left = result.report.left_for_next_check or None
        failed = [
            {"name": i.name, "summary": i.summary[:300], "evidence": i.evidence[:3], "items": len(i.evidence)}
            for i in result.report.invariants
            if not i.passed
        ]
    return CheckRun(
        n,
        started.isoformat(timespec="milliseconds"),
        round(time.monotonic() - clock, 2),
        outcome,
        failed,
        error,
        details,
        left,
    )


def run_live(
    rate: float = 5.0, checks: int = 20, seed: int = 7, warmup_seconds: float = 5.0, settle_timeout: float = 30.0
) -> UnderTrafficRun:
    if checks < 1:
        raise DemoError("run at least one check")
    settings = traffic.Settings(rate=rate, seed=seed)
    generator = traffic.TrafficGenerator(settings)
    generator.start()
    try:
        time.sleep(warmup_seconds)
        runs = [one_check(n, settle_timeout) for n in range(1, checks + 1)]
    finally:
        generator.close()  # nothing new; the requests already running finish
        generator.join()
    return UnderTrafficRun(
        settings=settings,
        warmup_seconds=warmup_seconds,
        checks=runs,
        quiet=one_check(0, settle_timeout),
        traffic=generator.stats.to_dict(),
        behavior_note=generator.behavior_note,
    )


def _line(c: CheckRun) -> str:
    when = c.started_utc[11:19]
    head = f"  #{c.n:<3} {when}  {c.outcome.upper():<13} {c.seconds:5.2f} s"
    if c.outcome == "failed":
        return f"{head}  " + "; ".join(f"{f['name']}: {f['summary'][:150]}" for f in c.failed)
    if c.error:
        return f"{head}  {c.error[:150]}"
    if c.left_for_next_check:
        left = c.left_for_next_check
        return (
            f"{head}  left for the next check: {left['provider_events']} events, {left['provider_charges']} charges,"
            f" {left.get('provider_refunds', 0)} refunds"
        )
    return head


def left_in_total(checks: list[CheckRun]) -> str:
    """How much each check left out as newer than its cutoff: the traffic in flight it did not mistake for loss."""
    counts = [c.left_for_next_check for c in checks if c.left_for_next_check]
    if not counts:
        return "No check left anything for the next one."
    events = [left["provider_events"] for left in counts]
    return (
        f"Newer than their cutoffs, left for the next check: {sum(events)} provider events in all, {min(events)} to"
        f" {max(events)} per check ({len(counts)} checks)."
    )


def render(run: UnderTrafficRun) -> str:
    s = run.settings
    counted = run.counted()
    out = [
        "Klaxon week 2: the money check, run while synthetic traffic flows",
        f"REAL RUN on this Mac's Docker Compose stack. Traffic run {s.run_id}, seed {s.seed}: {s.rate:g} payments"
        f" per second, provider noise lose_ack_rate {s.lose_ack_rate:g}, lost_answer_rate {s.lost_answer_rate:g}.",
        "Synthetic data, simulated provider, no real money. It measures the money check; it is not the agent.",
        "",
        f"Checks while the traffic flows (after {run.warmup_seconds:g} s of warm-up)",
        *(_line(c) for c in run.checks),
        "",
        "The traffic generator",
        *traffic.summary(run.traffic["counts"]),
        f"  {run.behavior_note}",
        "",
        "One more check, after the traffic stopped",
        _line(run.quiet),
        "",
        f"Under traffic: {len(run.checks)} checks, {counted['passed']} passed, {counted['failed']} failed,"
        f" {counted['could not run']} could not run. Quiet stack: {run.quiet.outcome}.",
        left_in_total(run.checks),
        f"RESULT: {'every check passed' if run.passed else 'NOT every check passed'}",
    ]
    return "\n".join(out)


def evidence(run: UnderTrafficRun, command: str, now: datetime) -> dict:
    return {
        "label": LABEL,
        "scenario": SCENARIO,
        **provenance(command, now),
        "every_check_passed": run.passed,
        "traffic_settings": asdict(run.settings),
        "warmup_seconds": run.warmup_seconds,
        "counted": run.counted(),
        "checks": [asdict(c) for c in run.checks],
        "quiet_check": asdict(run.quiet),
        "traffic": run.traffic,
        "provider_behavior": run.behavior_note,
        "printed_output": render(run).splitlines(),
    }


def write_evidence(run: UnderTrafficRun, directory: Path, command: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    directory.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{now.astimezone(timezone.utc):%Y-%m-%dT%H%M%SZ}_{SCENARIO}_rate{run.settings.rate:g}_seed{run.settings.seed}"
    )
    path = directory / f"{stem}.json"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}.json"
        n += 1
    path.write_text(json.dumps(evidence(run, command, now), indent=2, default=str) + "\n")
    return path
