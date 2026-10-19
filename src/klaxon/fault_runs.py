"""Klaxon's first loop on the faults: from an alert the stack raised, to a diagnosis compared with the known cause.

A test harness, like klaxon.faults and klaxon.traffic: it drives this Mac's stack, and Klaxon's loop
(klaxon.agent.loop) does the investigating. One run, live:

1. Before the stack is touched: the model id is checked, Claude Code is found, and the tool server must list the
   loop's tools. A run that could not investigate never injects anything.
2. Traffic on: synthetic customers (klaxon.traffic), 3 payments per second by default, then a minute's warm-up.
3. The alert rules (klaxon.alerts) must read every signal, with no condition holding, at two evaluations in a row,
   and the money check must pass. A stack that is already unwell would make the run mean nothing: then no fault is
   injected.
4. The fault is injected (klaxon.faults).
5. The rules are evaluated until one fires, for at most 3 minutes: 15 s apart, plus the time each evaluation takes
   (its money check may wait up to 15 s). They know nothing of the fault: the alert the loop gets is the one they
   raised from what the stack showed, unchanged.
6. The loop investigates that alert, live: Claude on Aravind's seat, Klaxon's read-only tools, within its budgets.
   The traffic keeps flowing and the fault stays in place, as in a real incident.
7. Always, whatever happened: the fault is cleared (its cause removed: a rollback, or the outage ending). Then the
   rules start their counts again and are evaluated once more, after 90 s, and the traffic stops.
8. The diagnosis is compared with the cause the fault records, by a rule (klaxon.grading), not by a person or a
   model; the evidence says so.

Everything goes into one folder in evidence/week3/: the loop's trace, model recording and tool results (make
replay-run replays it offline), and run.json: the fault and its recorded cause, every evaluation of the rules (when
it began and how long it took, since the traffic started), the alert, the timings, the traffic, the loop's result,
and the comparison. Its label is built from what the run did: a run that stopped before the fault, the alert or the
loop says so, and claims nothing it did not do.

Also here: `quiet` runs the traffic with no fault and evaluates the rules: each evaluation must be quiet (every
signal read, no condition holding, the money check passed), or the run fails; `grade` compares a saved run's
diagnosis with its fault's cause again, offline.

Run: python -m klaxon.fault_runs run FAULT [--steps N --tool-calls N --seconds S]   live, with Aravind's OK each time
     python -m klaxon.fault_runs quiet [--minutes M]                                  traffic only: injects nothing
     python -m klaxon.fault_runs grade FOLDER                                         offline
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from klaxon import alerts, faults, grading, traffic
from klaxon.agent import loop
from klaxon.agent.claude_code import DEFAULT_MODEL
from klaxon.agent.loop import Alert, Budgets, RunResult
from klaxon.agent.mcp_client import McpClient, McpError
from klaxon.agent.model import ModelError
from klaxon.agent.tools import clean_text
from klaxon.agent.trace import read_trace
from klaxon.patient.stack import REPO, StackError

QUIET_LABEL = (
    "Real run on this Mac's Docker Compose stack: synthetic traffic from klaxon.traffic and no fault, with the alert"
    " rules of klaxon.alerts evaluated again and again. No model was called. Synthetic data, a simulated provider."
)
# At the top of the loop's own files (its model recording and tool results), which exist only once the loop has run.
WHAT = (
    "One run of Klaxon's first loop on a fault injected on purpose: the alert the rules raised, Claude on Aravind's"
    " seat, read-only tools on this Mac's stack."
)
RUN_FILE = "run.json"
WARMUP_SECONDS = 60.0  # traffic before the rules are read: their windows are a minute long
QUIET_EVALUATIONS = 2  # before the fault: every rule read, none holding, this many evaluations in a row
ALERT_WITHIN_SECONDS = 180.0  # after the injection: no alert by then, and the run stops without a loop
AFTER_SECONDS = 90.0  # after clearing: long enough for the rules' one-minute windows to move past the fault


@dataclass
class Record:
    """What a run did, filled in as it goes, so that a run that stops early still leaves its evidence."""

    fault: dict  # its name, title, recorded cause, expected symptoms and remedy
    traffic_settings: dict
    budgets: dict
    stopped: str = "did not finish"
    timings: dict = field(default_factory=dict)  # seconds since the traffic started
    evaluations: list[dict] = field(default_factory=list)  # every evaluation of the rules: its phase, began, took
    injected: dict | None = None
    cleared: dict | None = None
    alert: dict | None = None
    loop: dict | None = None
    comparison: dict | None = None
    traffic: dict | None = None
    provider_behavior: str | None = None


@dataclass
class Stack:
    """What a run drives, all on this Mac. Tests replace each part."""

    local: faults.LocalStack
    alerting: alerts.Alerting
    new_traffic: Callable[[traffic.Settings], traffic.TrafficGenerator] = traffic.TrafficGenerator
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


def not_quiet(evaluation: alerts.Evaluation) -> str | None:
    """Why the stack is not ready for a fault, or None: every rule must have read its signal, with no condition
    holding, and the money check must have passed."""
    if evaluation.problems:
        return "; ".join(evaluation.problems)
    held = [f"{check.rule} ({check.subject})" for check in evaluation.checks if check.breached]
    if held:
        return f"a rule's condition already holds: {', '.join(held)}"
    return unseen(evaluation)


def unseen(evaluation: alerts.Evaluation) -> str | None:
    """What the rules could not see at this evaluation, or None: a source not read, a signal with no data, or a
    money check that gave no verdict (passed or failed)."""
    if evaluation.problems:
        return "; ".join(evaluation.problems)
    read = {(check.rule, check.subject): check.numbers for check in evaluation.checks}
    missing = []
    if (read.get(("PaymentsLatencyHigh", "payments-api")) or {}).get("p95_seconds") is None:
        missing.append("the p95 latency")
    if (read.get(("PaymentsErrorsHigh", "payments-api")) or {}).get("share") is None:
        missing.append("the share of 5xx answers")
    for group in alerts.CONSUMER_GROUPS.values():
        if (read.get(("ConsumerLagHigh", group)) or {}).get("messages_behind") is None:
            missing.append(f"the {group} group's lag")
    if missing:
        return f"no data for {', '.join(missing)}: is the traffic reaching the stack?"
    outcome = (read.get(("MoneyCheckFailed", "money check")) or {}).get("outcome")
    if outcome not in ("passed", "failed"):  # a failed check is a condition held, which the rule itself reports
        return f"the money check did not pass: {outcome}"
    return None


def run(
    fault: faults.Fault,
    record: Record,
    stack: Stack,
    settings: traffic.Settings,
    investigate: Callable[[Alert], RunResult],
    warmup_seconds: float = WARMUP_SECONDS,
    alert_within_seconds: float = ALERT_WITHIN_SECONDS,
    after_seconds: float = AFTER_SECONDS,
    every_seconds: float = alerts.EVERY_SECONDS,
) -> Record:
    """One run, filling in record as it goes. The fault is always cleared once its injection began, and the
    traffic always stopped, whatever happens; an error is raised after both."""
    generator = stack.new_traffic(settings)
    generator.start()  # a provider out of reach raises here, before any traffic
    started = stack.clock()

    def since() -> float:
        return round(stack.clock() - started, 1)

    def evaluate(phase: str) -> alerts.Evaluation:
        began = since()
        evaluation = stack.alerting.evaluate()
        record.evaluations.append(
            {"phase": phase, "began": began, "took": round(since() - began, 1)} | evaluation.to_dict()
        )
        return evaluation

    try:
        stack.sleep(warmup_seconds)
        for n in range(QUIET_EVALUATIONS):
            if n:
                stack.sleep(every_seconds)
            unwell = not_quiet(evaluate("before"))
            if unwell is not None:
                record.stopped = f"not quiet before the fault, so none was injected: {unwell}"
                return record
        injected_at = record.timings["injected_at"] = since()
        try:
            record.injected = fault.inject(stack.local)
            record.timings["injection_took"] = round(since() - injected_at, 1)
            alert = None
            while alert is None and since() - injected_at < alert_within_seconds:
                stack.sleep(every_seconds)
                alert = evaluate("during").alert
            if alert is None:
                record.stopped = f"no alert fired within {alert_within_seconds:g} s of the injection"
                return record
            record.alert = asdict(alert)
            record.timings["alert_evaluation_began"] = record.evaluations[-1]["began"]  # as its alert's fired_at
            record.timings["loop_started_at"] = since()
            result = investigate(alert)  # the alert as the rules raised it: the loop's only goal
            record.timings["loop_ended_at"] = since()
            record.loop = summary(result)
            record.comparison = grading.compare(fault.name, record.loop["diagnosis"])
            record.stopped = f"the loop stopped: {result.reason}"
        finally:
            record.cleared = fault.clear(stack.local)  # whatever happened: never leave the fault's cause in place
            record.timings["cleared_at"] = since()
        stack.alerting.reset()  # read the stack afresh: no count carried over from the fault
        stack.sleep(after_seconds)
        evaluate("after")
    finally:
        generator.close()
        generator.join()
        record.traffic = generator.stats.to_dict()
        record.provider_behavior = generator.behavior_note
    return record


def summary(result: RunResult) -> dict:
    return {
        "reason": result.reason,
        "detail": result.detail,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "seconds": result.seconds,
        "diagnosis": None if result.diagnosis is None else result.diagnosis.as_dict(),
        "trace": result.trace.name,
    }


def quiet(
    alerting: alerts.Alerting,
    settings: traffic.Settings,
    minutes: float,
    new_traffic: Callable[[traffic.Settings], traffic.TrafficGenerator] = traffic.TrafficGenerator,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    warmup_seconds: float = WARMUP_SECONDS,
    every_seconds: float = alerts.EVERY_SECONDS,
) -> dict:
    """Traffic and no fault: the rules evaluated again and again for minutes, 15 s apart. On a healthy stack no alert
    may fire, and every evaluation must see (every source read, data for every rule, a money check with a verdict).
    A condition that holds once is counted, not failed: the rules are built to let one blip pass."""
    generator = new_traffic(settings)
    generator.start()
    started = clock()
    evaluations = []
    try:
        sleep(warmup_seconds)
        deadline = clock() + minutes * 60
        while True:
            began = round(clock() - started, 1)
            evaluation = alerting.evaluate()
            blind = unseen(evaluation)
            evaluations.append({"began": began, "blind": blind} | evaluation.to_dict())
            if clock() + every_seconds > deadline:
                break
            sleep(every_seconds)
    finally:
        generator.close()
        generator.join()
    return {
        "traffic_settings": asdict(settings),
        "minutes": minutes,
        "evaluations": evaluations,
        "alerts_fired": sum(1 for e in evaluations if e["alert"] is not None),
        "conditions_held": sum(1 for e in evaluations for check in e["checks"] if check["breached"]),
        "evaluations_with_problems": sum(1 for e in evaluations if e["problems"]),
        "evaluations_blind": sum(1 for e in evaluations if e["blind"] is not None),
        "traffic": generator.stats.to_dict(),
        "provider_behavior": generator.behavior_note,
    }


# What a run prints.


def brief(evaluation: Mapping) -> str:
    """One evaluation in one line: the numbers each rule read, and what fires."""
    by_key = {(check["rule"], check["subject"]): check for check in evaluation["checks"]}

    def number(rule: str, subject: str, key: str) -> object:
        value = (by_key.get((rule, subject)) or {}).get("numbers", {}).get(key)
        return "-" if value is None else value

    lags = ", ".join(
        f"{group} {number('ConsumerLagHigh', group, 'messages_behind')}" for group in alerts.CONSUMER_GROUPS.values()
    )
    firing = [f"{check['rule']} ({check['subject']})" for check in evaluation["checks"] if check["firing"]]
    line = (
        f"p95 {number('PaymentsLatencyHigh', 'payments-api', 'p95_seconds')} s;"
        f" 5xx share {number('PaymentsErrorsHigh', 'payments-api', 'share')};"
        f" lag {lags}; money check {number('MoneyCheckFailed', 'money check', 'outcome')};"
        f" firing: {', '.join(firing) or 'none'}"
    )
    if evaluation["problems"]:
        line += f"; problems: {'; '.join(evaluation['problems'])}"
    return line


def label(record: Record) -> str:
    """What this run's evidence is, from what the run did: it never claims a step the run did not reach."""
    if record.traffic is None:
        return (
            "Real run on this Mac's Docker Compose stack, which stopped before its synthetic traffic began: no fault"
            f" was injected and no model was called. The run stopped: {record.stopped}."
        )
    said = [
        "Real run on this Mac's Docker Compose stack, with synthetic traffic from klaxon.traffic: synthetic data, a"
        " simulated provider, no real money."
    ]
    if "injected_at" not in record.timings:
        return " ".join([*said, f"No fault was injected. The run stopped: {record.stopped}."])
    if record.injected is None:
        said.append(
            f"An injection of one fault from klaxon.faults, {record.fault['name']}, began on purpose and failed."
        )
    else:
        said.append(f"One fault from klaxon.faults, {record.fault['name']}, was injected on purpose.")
    if record.cleared is None:
        said.append("Clearing it failed, so it may still be in place.")
    if record.injected is None:
        return " ".join([*said, f"The run stopped: {record.stopped}."])
    if record.alert is None:
        return " ".join([*said, f"No alert fired. The run stopped: {record.stopped}."])
    said.append("The alert was raised by klaxon.alerts from what the stack showed.")
    if "loop_started_at" in record.timings:
        if record.loop is None:
            said.append(
                f"Klaxon's loop started live on it, through Claude Code on Aravind's seat, and did not finish: {record.stopped}."
            )
        else:
            said.append(
                f"Klaxon's loop ran live on it, through Claude Code on Aravind's seat, and stopped on {record.loop['reason']}"
                f" after {record.loop['steps']} steps; any reply it got is a recorded response, not a simulation."
            )
    if record.comparison is not None and record.comparison["verdict"] != "no diagnosis":
        said.append(
            "The diagnosis was compared with the fault's recorded cause by a rule (klaxon.grading), not by a person or"
            " a model."
        )
    return " ".join(said)


def render(record: Record) -> str:
    f = record.fault
    lines = [
        f"Klaxon week 3: a run of the first loop on a fault: {f['name']}",
        label(record),
        "",
        f"The fault: {f['title']}",
        f"  recorded cause: {f['cause']}",
        "",
        "The alert rules (t: when each evaluation began, from the start of the traffic; rates and p95 are over the"
        " last minute)",
        *(f"  {e['phase']:<6} t={e['began']:>6.1f} s  {brief(e)}" for e in record.evaluations),
    ]
    if record.injected is not None:
        lines.append(
            f"  the fault was injected at t={record.timings['injected_at']:g} s: {json.dumps(record.injected)}"
        )
    elif "injected_at" in record.timings:
        lines.append(f"  an injection began at t={record.timings['injected_at']:g} s and failed")
    if record.alert is not None:
        lines += ["", f"Alert {record.alert['name']}, fired at {record.alert['fired_at']}: {record.alert['summary']}"]
    if record.loop is not None:
        run_ = record.loop
        lines += [
            "",
            f"The loop stopped on {run_['reason']} after {run_['steps']} steps, {run_['tool_calls']} tool calls and"
            f" {run_['seconds']:.1f} s.",
        ]
        found = run_["diagnosis"]
        if found is not None:
            fix = found["proposed_fix"]
            lines += [
                f"  hypothesis: {clean_text(found['hypothesis'])}",
                f"  confidence: {found['confidence']:g}",
                f"  proposed fix (not applied): {fix['kind']} {clean_text(fix['target'], 200)}: {clean_text(fix['summary'])}",
            ]
    if record.comparison is not None:
        lines += ["", *grading.render(record.comparison)]
    if record.cleared is not None:
        lines += ["", f"Cleared at t={record.timings['cleared_at']:g} s: {json.dumps(record.cleared)}"]
    lines += ["", f"Stopped: {record.stopped}"]
    return "\n".join(lines)


def evidence(record: Record, header: Mapping) -> dict:
    return {"label": label(record), **header, **asdict(record), "printed_output": render(record).splitlines()}


# The command line.


def _provenance(command: str) -> dict:
    from klaxon.demo.live import provenance  # the stack's images and versions, as in week 2's evidence

    return provenance(clean_text(command, 2000), datetime.now(timezone.utc))


def preflight(model_id: str, client: Callable[[], McpClient] = McpClient) -> Path:
    """Before the stack is touched: the model id, Claude Code, and the read-only tools the loop will use."""
    binary = loop.find_live_model(model_id)
    tools = client()
    try:
        loop.allowed_tools(tools.open())
    finally:
        tools.close()
    return binary


def _run(args: argparse.Namespace, argv: Sequence[str]) -> int:
    fault = faults.get(args.fault)
    try:  # before any folder is made: a run that cannot start leaves nothing behind, and touches nothing
        budgets = Budgets(steps=args.steps, tool_calls=args.tool_calls, seconds=args.seconds)
        settings = traffic.Settings(rate=args.rate, seed=args.seed)
        binary = preflight(args.model)
        parts = Stack(faults.LocalStack(), alerts.Alerting())
        header = {"fault_name": fault.name} | _provenance(shlex.join(["python", "-m", "klaxon.fault_runs", *argv]))
    except (ValueError, ModelError, McpError, loop.ToolsRefused, StackError) as exc:
        print(f"Not started, and nothing was touched: {clean_text(str(exc), 300)}", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    folder = loop._new_folder(args.evidence_dir, f"{stamp}_fault-run_{fault.name}")
    record = Record(fault.describe(), asdict(settings), asdict(budgets))

    def investigate(alert: Alert) -> RunResult:
        run_header = header | {"what": WHAT, "alert": asdict(alert), "budgets": asdict(budgets)}
        return loop.investigate_live(alert, budgets, folder, run_header, args.model, binary)

    code = 0
    try:
        run(fault, record, parts, settings, investigate)
    except KeyboardInterrupt:
        record.stopped, code = "interrupted", 130
    except Exception as exc:  # the fault was cleared and the traffic stopped, unless this is where it failed
        record.stopped, code = f"error: {type(exc).__name__}: {clean_text(str(exc), 300)}", 2
    (folder / RUN_FILE).write_text(json.dumps(evidence(record, header), indent=2, default=str) + "\n")
    print(render(record))
    print(f"\nSaved in {clean_text(str(folder))}")
    if "injected_at" in record.timings and record.cleared is None:
        print(f"\nTHE FAULT MAY STILL BE IN PLACE: make fault-clear FAULT={fault.name}", file=sys.stderr)
    if code:
        return code
    return 0 if record.loop is not None and record.loop["reason"] == "diagnosed" else 1


def _quiet(args: argparse.Namespace, argv: Sequence[str]) -> int:
    try:
        settings = traffic.Settings(rate=args.rate, seed=args.seed)
        alerting = alerts.Alerting()
        header = _provenance(shlex.join(["python", "-m", "klaxon.fault_runs", *argv]))
        found = quiet(alerting, settings, args.minutes)
    except (ValueError, StackError, httpx.HTTPError) as exc:
        print(f"Could not run: {clean_text(str(exc), 300)}. Is the stack up (make up)?", file=sys.stderr)
        return 2
    out = {"label": QUIET_LABEL, **header, **found}
    for evaluation in found["evaluations"]:
        print(f"t={evaluation['began']:>6.1f} s  {brief(evaluation)}")
    print(
        f"{len(found['evaluations'])} evaluations: {found['alerts_fired']} with an alert, {found['conditions_held']}"
        f" conditions held, {found['evaluations_with_problems']} with a problem, {found['evaluations_blind']} blind (a"
        " source not read, a rule with no data, or a money check with no verdict)"
    )
    print(*traffic.summary(found["traffic"]["counts"]), sep="\n")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    path = args.evidence_dir / f"{stamp}_alerts-under-traffic_rate{args.rate:g}_seed{args.seed}.json"
    path.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(f"Saved: {clean_text(str(path))}")
    return 0 if found["alerts_fired"] == 0 and found["evaluations_blind"] == 0 else 1


def _grade(folder: Path) -> int:
    try:
        saved = json.loads((folder / RUN_FILE).read_text())
        stop = read_trace(folder / loop.TRACE)[-1]
        comparison = grading.compare(saved["fault"]["name"], stop.get("diagnosis"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            f"Cannot grade {clean_text(str(folder))}: {type(exc).__name__}: {clean_text(str(exc), 300)}",
            file=sys.stderr,
        )
        return 2
    print("\n".join(grading.render(comparison)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="python -m klaxon.fault_runs", description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action", required=True)
    live = actions.add_parser("run", help="one live run on a fault: Claude on Aravind's seat, this Mac's stack")
    live.add_argument("fault", choices=sorted(faults.FAULTS))
    live.add_argument("--steps", type=int, default=Budgets.steps)
    live.add_argument("--tool-calls", type=int, default=Budgets.tool_calls)
    live.add_argument("--seconds", type=float, default=Budgets.seconds)
    live.add_argument("--model", default=DEFAULT_MODEL)
    calm = actions.add_parser("quiet", help="traffic and no fault: do the alert rules stay quiet?")
    calm.add_argument("--minutes", type=float, default=5.0)
    for sub in (live, calm):
        sub.add_argument("--rate", type=float, default=3.0, help="new payments per second, on average")
        sub.add_argument("--seed", type=int, default=7)
        sub.add_argument("--evidence-dir", type=Path, default=REPO / "evidence" / "week3")
    again = actions.add_parser("grade", help="compare a saved run's diagnosis with its fault's cause; offline")
    again.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    if args.action == "grade":
        return _grade(args.folder)
    if args.action == "quiet":
        return _quiet(args, argv)
    return _run(args, argv)


if __name__ == "__main__":
    sys.exit(main())
