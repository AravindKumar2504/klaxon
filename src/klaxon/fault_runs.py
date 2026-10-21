"""Klaxon on the faults: from an alert the stack raised, to a diagnosis, to an action a person approved, verified.

A test harness, like klaxon.faults and klaxon.traffic: it drives this Mac's stack, and Klaxon (klaxon.agent) does
the investigating, the asking and the acting. One run, live (week 3's steps, then week 4's at 6a to 6d):

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
   The traffic keeps flowing and the fault stays in place, as in a real incident. It is offered one write action,
   rollback_deploy, which its diagnosis may name; it runs none.
   6a. If the diagnosis names it, the money check is read (the damage at the request), and the approval gate
       (klaxon.agent.gate) plans it, saves a request and waits, 10 minutes at most, for `make approve` in Aravind's
       own terminal. Only an approved plan runs.
   6b. After an action that ran, code verifies it (klaxon.agent.verify): health, and the money right after the
       action (the damage at the action) and a minute later (the damage after it).
   6c. A failed verification makes Klaxon propose undoing its action, through the gate. In this system the gate
       refuses every undo (it would deploy a known-bad setting) and says that undoing it is a person's call.
   6d. The fault stays in place through all of it: the harness never clears it before Klaxon acts.
7. Always, whatever happened: the fault is cleared (its cause removed: a rollback, or the outage ending), the
   safety net. After Klaxon's own rollback the clearing finds the settings already running and changes nothing. The
   deploy log then says who removed the cause: Klaxon, with a person's approval, or the harness. Then the rules start
   their counts again and are evaluated once more, after 90 s, and the traffic stops.
8. The diagnosis is compared with the cause the fault records, by a rule (klaxon.grading), not by a person or a
   model; the evidence says so.

Everything goes into one folder in evidence/week4/: the loop's trace, model recording and tool results (make
replay-run replays it offline), the gate's request, decision and log, and run.json: the fault and its recorded cause,
every evaluation of the rules (when it began and how long it took, since the traffic started), the alert, the
timings, the traffic, the loop's result, the comparison, the gate's outcome, the verification, the damage at the
alert, at the request, at the action and after it, the deploys made during the run, and who removed the cause. Its
label is built from what the run did: a run that stopped before the fault, the alert, the loop or the gate says so,
and claims nothing it did not do. With --no-actions, no action is offered: week 3's run, diagnosis only.

Also here: `quiet` runs the traffic with no fault and evaluates the rules: each evaluation must be quiet (every
signal read, no condition holding, the money check passed), or the run fails; `grade` compares a saved run's
diagnosis with its fault's cause again, offline.

Run: python -m klaxon.fault_runs run FAULT [--steps N --tool-calls N --seconds S]   live, with Aravind's OK each time
         [--no-actions]                                                               diagnosis only, as in week 3
     python -m klaxon.fault_runs quiet [--minutes M]                                  traffic only: injects nothing
     python -m klaxon.fault_runs grade FOLDER                                         offline
"""

from __future__ import annotations

import argparse
import json
import shlex
import signal
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from klaxon import alerts, faults, grading, traffic
from klaxon.agent import catalog, gate, loop, verify
from klaxon.agent.claude_code import DEFAULT_MODEL
from klaxon.agent.contract import Diagnosis
from klaxon.agent.loop import Alert, Budgets, RunResult
from klaxon.agent.mcp_client import McpClient, McpError
from klaxon.agent.model import ModelError
from klaxon.agent.tools import clean_text
from klaxon.agent.trace import read_trace
from klaxon.checks import live
from klaxon.checks.money import format_cents
from klaxon.patient.deploys import Deploy, Deployer
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
GATED_WHAT = (
    "One gated run of Klaxon on a fault injected on purpose: the alert the rules raised, Claude on Aravind's seat,"
    " read-only tools on this Mac's stack, and one write action offered, which runs only if a person approves it at"
    " the gate."
)
RUN_FILE = "run.json"
OFFERED = (catalog.ROLLBACK_DEPLOY,)  # the write actions a gated run offers the loop
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
    # A gated run (week 4): what came of the diagnosis at the gate, the verification, any undo proposed, the damage at
    # the alert, at the request, at the action and after it, the deploys made during the run, and who removed the cause.
    gated: bool = False
    gate: dict | None = None
    verification: dict | None = None
    undo: dict | None = None
    damage: dict = field(default_factory=dict)
    deploys: list[dict] | None = None
    removed_by: str | None = None


@dataclass
class Gated:
    """Klaxon's part after its diagnosis, in a gated run: the gate, verification, and any undo it proposes. The
    command builds the real ones (klaxon_parts); tests replace each."""

    read_money: Callable[[], verify.Reading]  # the money check, read once a plan is made, right before its request
    # plan, ask, wait for make approve, run if approved; the third argument is called right before the request
    act: Callable[[Alert, Diagnosis, Callable[[], None]], gate.Outcome]
    decided: Callable[[], dict | None]  # the decision the run took, and whether make approve wrote it
    verify_action: Callable[[Callable[[], alerts.Evaluation]], verify.Verification]  # given the rules, fresh
    propose_undo: Callable[[gate.Outcome, verify.Verification], gate.Outcome]
    deploys: Callable[[], list[Deploy]]  # the deploy log, oldest first


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
    gated: Gated | None = None,
) -> Record:
    """One run, filling in record as it goes. The fault is always cleared once its injection began, and the
    traffic always stopped, whatever happens; an error is raised after both. With gated, Klaxon may act on its
    diagnosis, through the gate, before the fault is cleared."""
    record.gated = gated is not None  # first: a run whose traffic never starts is still said as gated
    generator = stack.new_traffic(settings)
    generator.start()  # a provider out of reach raises here, before any traffic
    started = stack.clock()
    marks: dict[str, int | None] = {}  # the deploy log's last line before the fault, and before its clearing

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
        if gated is not None:
            marks["start"] = _last_seq(gated)
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
            if gated is not None:
                record.damage["at_alert"] = at_alert(record.evaluations[-1])
            record.timings["loop_started_at"] = since()
            result = investigate(alert)  # the alert as the rules raised it: the loop's only goal
            record.timings["loop_ended_at"] = since()
            record.loop = summary(result)
            record.comparison = grading.compare(fault.name, record.loop["diagnosis"])
            record.stopped = f"the loop stopped: {result.reason}"
            if gated is not None and result.diagnosis is not None:  # the fault stays in place while Klaxon acts
                _act(result.diagnosis, alert, gated, record, since, stack.alerting, lambda: evaluate("verify"))
        finally:
            if gated is not None:
                marks["clearing"] = _last_seq(gated)
            record.cleared = fault.clear(stack.local)  # whatever happened: never leave the fault's cause in place
            record.timings["cleared_at"] = since()
        stack.alerting.reset()  # read the stack afresh: no count carried over from the fault
        stack.sleep(after_seconds)
        evaluate("after")
    finally:
        try:
            if gated is not None and "injected_at" in record.timings:
                _who_removed(record, gated, marks)
        finally:
            generator.close()
            generator.join()
            record.traffic = generator.stats.to_dict()
            record.provider_behavior = generator.behavior_note
    return record


def _act(
    diagnosis: Diagnosis,
    alert: Alert,
    gated: Gated,
    record: Record,
    since: Callable[[], float],
    alerting: alerts.Alerting,
    rules: Callable[[], alerts.Evaluation],
) -> None:
    """Klaxon's part after its diagnosis: the gate (which asks only for a named action it could plan), verification
    of an action that ran, and an undo proposed when verification failed."""

    def at_request() -> None:  # the gate made a plan, and is about to ask: the damage as it is asked
        began = since()
        record.damage["at_request"] = gated.read_money().to_dict() | {"began": began, "took": round(since() - began, 1)}

    record.timings["gate_began_at"] = since()
    outcome = gated.act(alert, diagnosis, at_request)  # waits here, up to 10 minutes, for make approve
    record.timings["gate_ended_at"] = since()
    try:
        decided = gated.decided() if outcome.request_id else None
    except gate.GateError as exc:  # the outcome is known all the same: keep it, and say what is not
        decided = {"error": str(exc)}
    record.gate = asdict(outcome) | {"decided": decided}
    if outcome.result != "done":
        return
    alerting.reset()  # verification reads the rules afresh: no count carried over from before the action
    record.timings["verify_began_at"] = since()
    found = gated.verify_action(rules)
    record.timings["verify_ended_at"] = since()
    record.verification = found.to_dict()
    record.damage["at_action"] = found.at_action.to_dict()
    record.damage["after_action"] = found.after.to_dict()
    if found.verdict == "failed":
        record.undo = asdict(gated.propose_undo(outcome, found))


def _last_seq(gated: Gated) -> int | None:
    """The number of the deploy log's last line; None when the log cannot be read. Never raises: it is read right
    before the fault is cleared, and nothing may stand in the clearing's way."""
    try:
        records = gated.deploys()
    except Exception:
        return None
    return records[-1].seq if records else 0


def _who_removed(record: Record, gated: Gated, marks: Mapping[str, int | None]) -> None:
    """The deploys made during the run, from the deploy log, and who removed the fault's cause: Klaxon's rollback
    (by the request a person approved) or the harness's clearing. Never raises: the traffic must still stop."""
    start, clearing = marks.get("start"), marks.get("clearing")
    try:
        lines = [line for line in gated.deploys() if start is not None and line.seq > start]
    except Exception as exc:
        record.removed_by = f"not known: the deploy log could not be read ({type(exc).__name__})"
        return
    record.deploys = [
        {
            "seq": line.seq,
            "at": line.at,
            "service": line.service,
            "by": line.by,
            "container": line.container[:12],
            "settings": line.settings,
            "replaced_settings": line.replaced_settings,
        }
        for line in lines
    ]
    klaxon = [line for line in lines if line.by.startswith(gate.BY)]
    cleared = [line for line in lines if clearing is not None and line.seq > clearing]
    if start is None or clearing is None:
        record.removed_by = "not known: the deploy log could not be read"
    elif record.cleared is None:
        record.removed_by = "not known: the harness's clearing failed, so the fault may still be in place"
    elif "redeployed" not in record.cleared:  # its cause is not a deploy (an outage): only its clearing removes it
        record.removed_by = "the harness's clearing: this fault's cause is not a deploy" + (
            ", so Klaxon's rollback did not remove it" if klaxon else ""
        )
    elif cleared:
        record.removed_by = "the harness's clearing (klaxon.faults)" + (
            ", although Klaxon had rolled back: the clearing still found something to change" if klaxon else ""
        )
    elif klaxon:
        record.removed_by = (
            f"Klaxon: its rollback ({klaxon[-1].by}) removed the cause, and the harness's clearing then found the good"
            " settings running, and changed nothing"
        )
    else:
        record.removed_by = (
            "not known: the clearing found the good settings running, and the log has no deploy of Klaxon's"
        )


def at_alert(evaluation: Mapping) -> dict:
    """The damage the alert's own evaluation of the rules saw: their money check in numbers, and each group's lag."""
    by_key = {(check["rule"], check["subject"]): check for check in evaluation["checks"]}
    money = (by_key.get(("MoneyCheckFailed", "money check")) or {}).get("numbers") or {}
    lag = {
        group: (by_key.get(("ConsumerLagHigh", group)) or {}).get("numbers", {}).get("messages_behind")
        for group in alerts.CONSUMER_GROUPS.values()
    }
    return {
        "source": "the alert's own evaluation of the rules",
        "at_utc": evaluation["at_utc"],
        "began": evaluation["began"],
        "money_check": dict(money),
        "lag": lag,
    }


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
        offered = (
            ", offered one write action (rollback_deploy), which runs only if a person approves it at the gate"
            if record.gated
            else ""
        )
        if record.loop is None:
            said.append(
                f"Klaxon's loop started live on it, through Claude Code on Aravind's seat{offered}, and did not finish:"
                f" {record.stopped}."
            )
        else:
            said.append(
                f"Klaxon's loop ran live on it, through Claude Code on Aravind's seat{offered}, and stopped on"
                f" {record.loop['reason']} after {record.loop['steps']} steps; any reply it got is a recorded response,"
                " not a simulation."
            )
    if record.comparison is not None and record.comparison["verdict"] != "no diagnosis":
        said.append(
            "The diagnosis was compared with the fault's recorded cause by a rule (klaxon.grading), not by a person or"
            " a model."
        )
    if record.gated:
        said += _gated_label(record)
    return " ".join(said)


def _gated_label(record: Record) -> list[str]:
    """What a gated run did after the diagnosis: the gate, the verification, any undo, and who removed the cause."""
    said = []
    outcome = record.gate
    if outcome is None and "gate_began_at" in record.timings:
        said.append(f"Klaxon took its diagnosis to the approval gate, and the run stopped there: {record.stopped}.")
    elif outcome is not None:
        result, detail = outcome["result"], clean_text(outcome["detail"], 300)
        asked = "Klaxon asked at the approval gate to act on its diagnosis"
        decided = outcome.get("decided") or {}
        how = (
            "with make approve (the request's id typed at a terminal)"
            if decided.get("written_by_make_approve")
            else "by a decision that no line of make approve names"
        )
        if "error" in decided:
            how = "(the gate's log cannot be read, so who wrote the decision is not known)"
        said.append(
            {
                "no action": "Its diagnosis named no action, so nobody was asked to approve one.",
                "refused": f"Its diagnosis named an action that code refused to plan, so nobody was asked: {detail}.",
                "expired": f"{asked}; nobody answered within 10 minutes, which counts as denied, and nothing ran.",
                "denied": f"{asked}; the request was denied ({detail}), and nothing ran.",
                "not run": f"{asked}; the request was approved {how}, but the action did not run: {detail}.",
                "failed": f"{asked}; the request was approved {how}, and the action failed: {detail}.",
                "done": f"{asked}; the request was approved {how}, and the action ran: {detail}.",
            }.get(result, f"{asked}: {result}.")
        )
    if record.verification is not None:
        money = record.verification["money"]
        cause = {True: "the cause is removed", False: "the cause is not removed"}.get(
            money["cause_removed"], "whether the cause is removed is not known"
        )
        right = {True: "the money is right", False: "the money is not right"}.get(
            money["right"], "whether the money is right is not known"
        )
        said.append(
            f"Code, not the model, then verified it (klaxon.agent.verify): {record.verification['verdict']}; {cause},"
            f" and {right}."
        )
    elif "verify_began_at" in record.timings:
        said.append(f"Its verification began and did not finish: {record.stopped}.")
    if record.undo is not None:
        said.append(
            f"The verification failed, so Klaxon proposed undoing its action, and the gate refused it:"
            f" {clean_text(record.undo['detail'], 300)}"
        )
    if record.removed_by is not None:
        said.append(removed_sentence(record.removed_by))
    return said


NOT_KNOWN = "not known: "


def removed_sentence(removed_by: str) -> str:
    """Who removed the fault's cause, in a sentence: "not known" is never said as if it were someone."""
    if removed_by.startswith(NOT_KNOWN):
        return f"Who removed the fault's cause is not known: {removed_by[len(NOT_KNOWN) :]}."
    return f"The fault's cause was removed by {removed_by}."


def damage_line(entry: Mapping) -> str:
    """One of the run's damage readings in one line: the alert's own evaluation, or a money check of Klaxon's."""
    if "money_check" in entry:  # the alert's own evaluation of the rules
        money = entry["money_check"]
        text = f"money check {money.get('outcome')}"
        if money.get("outcome") == "failed":
            text += ": " + ", ".join(f"{name} {n}" for name, n in (money.get("failed") or {}).items())
            off = money.get("money_off_cents")
            if isinstance(off, int) and off:
                text += f"; our books {format_cents(abs(off))} {'ahead of' if off > 0 else 'behind'} the provider's"
        lags = ", ".join(f"{group} {n:g}" for group, n in entry["lag"].items() if isinstance(n, int | float))
        return text + (f"; lag {lags}" if lags else "")
    return verify.damage(verify.Reading.from_dict(entry))


DAMAGE_POINTS = (
    ("at_alert", "at the alert"),
    ("at_request", "at the request"),
    ("at_action", "at the action"),
    ("after_action", "after it"),
)


def _render_gated(record: Record) -> list[str]:
    lines = []
    outcome = record.gate
    if outcome is not None:
        lines += ["", f"The gate: {outcome['result']}: {clean_text(outcome['detail'], 500)}"]
        decided = outcome.get("decided")
        if outcome.get("request_id"):
            made = record.timings.get("gate_began_at")
            ended = record.timings.get("gate_ended_at")
            lines.append(
                f"  request {outcome['request_id']}: the gate began at t={made:g} s and was done at t={ended:g} s"
            )
        if decided and "error" in decided:
            lines.append(f"  decision: not known: {clean_text(decided['error'], 200)}")
        elif decided:
            by = (
                "make approve wrote it"
                if decided.get("written_by_make_approve")
                else "no line of make approve names it"
            )
            lines.append(f"  decision: {decided.get('decision')} at {decided.get('at')}, {decided.get('how')}; {by}")
    if record.verification is not None:
        lines += ["", f"Verification, by code and not by the model: {record.verification['verdict']}"]
        lines += [f"  {sentence}" for sentence in record.verification["said"]]
    if record.damage:
        lines += ["", "The damage, by the money check:"]
        for key, name in DAMAGE_POINTS:
            entry = record.damage.get(key)
            if entry is not None:
                lines.append(f"  {name:<15} {entry['at_utc']}  {damage_line(entry)}")
    if record.undo is not None:
        lines += ["", f"Undo: {record.undo['result']}: {clean_text(record.undo['detail'], 500)}"]
    if record.deploys:
        lines += ["", "Deploys during the run (the deploy log):"]
        for line in record.deploys:
            settings = ", ".join(f"{name}={value}" for name, value in (line["settings"] or {}).items())
            lines.append(f"  {line['seq']} {line['at']} {line['service']} by {clean_text(line['by'], 100)}: {settings}")
    if record.removed_by is not None:
        lines += ["", removed_sentence(record.removed_by)]
    return lines


def render(record: Record) -> str:
    f = record.fault
    title = (
        "Klaxon week 4: a gated run on a fault" if record.gated else "Klaxon week 3: a run of the first loop on a fault"
    )
    lines = [
        f"{title}: {f['name']}",
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
            how = "the model's words" if record.gated else "not applied"
            lines += [
                f"  hypothesis: {clean_text(found['hypothesis'])}",
                f"  confidence: {found['confidence']:g}",
                f"  proposed fix ({how}): {fix['kind']} {clean_text(fix['target'], 200)}: {clean_text(fix['summary'])}",
            ]
            named = found.get("fix_action")
            if named is not None:
                lines.append(
                    f"  action it names: {clean_text(named['name'], 64)}"
                    f" {clean_text(json.dumps(named['arguments']), 300)} (it runs only through the gate)"
                )
    if record.comparison is not None:
        lines += ["", *grading.render(record.comparison)]
    if record.gated:
        lines += _render_gated(record)
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


@contextmanager
def stops_as_interrupts() -> Iterator[None]:
    """While a run lasts, a stop signal does what Ctrl-C does: SIGTERM (a background task stopped) and SIGHUP (its
    terminal closed) raise KeyboardInterrupt, so the fault is still cleared, the traffic stopped and the evidence
    saved. Python's own default for them ends the process at once, with no finally run. A gated run can wait ten
    minutes at the gate, so this matters more than it did. A signal ignored on purpose (SIGHUP under nohup, to
    outlive a closed terminal) stays ignored, as Python leaves an ignored SIGINT."""

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(signal.Signals(signum).name)

    before = {}
    for stop in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(stop) is not signal.SIG_IGN:
            before[stop] = signal.signal(stop, interrupt)
    try:
        yield
    finally:
        for stop, handler in before.items():
            signal.signal(stop, handler)


def klaxon_parts(folder: Path, deployer: Deployer) -> Gated:
    """The real gate, verification and undo, for one run's folder, on this Mac's stack. deployer: the faults' own
    (klaxon.faults.LocalStack), so a fault's deploys and Klaxon's share one deploy log, and one lock."""

    def propose_undo(outcome: gate.Outcome, found: verify.Verification) -> gate.Outcome:
        return gate.propose_undo(folder, outcome, deployer, "the verification failed: " + " ".join(found.said))

    return Gated(
        read_money=lambda: verify.read(live.check_live, alerts.MONEY_SETTLE_SECONDS),
        act=lambda alert, diagnosis, asking: gate.act(folder, diagnosis, deployer, asdict(alert), before_asking=asking),
        decided=lambda: gate.decided(folder),
        verify_action=verify.verify,
        propose_undo=propose_undo,
        deploys=deployer.log.records,
    )


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
    gated_run = not args.no_actions
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    folder = loop._new_folder(args.evidence_dir, f"{stamp}_{'gated-run' if gated_run else 'fault-run'}_{fault.name}")
    record = Record(fault.describe(), asdict(settings), asdict(budgets))
    offered = OFFERED if gated_run else ()
    gated = klaxon_parts(folder, parts.local.deployer) if gated_run else None
    print(f"The run's folder: {clean_text(str(folder))}", flush=True)  # make approve needs it, while the run waits

    def investigate(alert: Alert) -> RunResult:
        what = GATED_WHAT if gated_run else WHAT
        run_header = header | {"what": what, "alert": asdict(alert), "budgets": asdict(budgets)}
        return loop.investigate_live(alert, budgets, folder, run_header, args.model, binary, actions=offered)

    code = 0
    try:
        with stops_as_interrupts():
            run(fault, record, parts, settings, investigate, gated=gated)
    except KeyboardInterrupt as exc:
        record.stopped, code = "interrupted" + (f" ({exc})" if str(exc) else ""), 130
    except Exception as exc:  # the fault was cleared and the traffic stopped, unless this is where it failed
        record.stopped, code = f"error: {type(exc).__name__}: {clean_text(str(exc), 300)}", 2
    (folder / RUN_FILE).write_text(json.dumps(evidence(record, header), indent=2, default=str) + "\n")
    print(render(record))
    print(f"\nSaved in {clean_text(str(folder))}")
    if "injected_at" in record.timings and record.cleared is None:
        print(f"\nTHE FAULT MAY STILL BE IN PLACE: make fault-clear FAULT={fault.name}", file=sys.stderr)
    if code:
        return code
    if record.loop is None or record.loop["reason"] != "diagnosed":
        return 1
    if record.gate is not None and record.gate["result"] in ("not run", "failed"):  # approved, and it did not run
        return 1
    return 1 if record.verification is not None and record.verification["verdict"] != "passed" else 0


def _quiet(args: argparse.Namespace, argv: Sequence[str]) -> int:
    try:
        settings = traffic.Settings(rate=args.rate, seed=args.seed)
        alerting = alerts.Alerting()
        header = _provenance(shlex.join(["python", "-m", "klaxon.fault_runs", *argv]))
        with stops_as_interrupts():
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
        version = grading.version_of(saved.get("comparison"))  # the version that graded it, so the verdict is the same
        comparison = grading.compare(saved["fault"]["name"], stop.get("diagnosis"), version)
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
    live.add_argument(
        "--no-actions", action="store_true", help="offer no write action: the loop only diagnoses, as in week 3"
    )
    calm = actions.add_parser("quiet", help="traffic and no fault: do the alert rules stay quiet?")
    calm.add_argument("--minutes", type=float, default=5.0)
    for sub in (live, calm):
        sub.add_argument("--rate", type=float, default=3.0, help="new payments per second, on average")
        sub.add_argument("--seed", type=int, default=7)
        sub.add_argument("--evidence-dir", type=Path, default=REPO / "evidence" / "week4")
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
