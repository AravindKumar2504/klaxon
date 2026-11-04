"""Klaxon evaluated (week 5): the same incidents many times, each run graded, measured and kept.

A test harness, like klaxon.fault_runs, which runs each fault case: Klaxon (klaxon.agent) does the investigating. Eight
cases, each with its right answer committed before any run on it (CASES, below; `make eval-cases` prints them):

    the six faults      the fault's recorded cause (klaxon.faults); the action that fits, its remedy's rollback (none
                        for the provider's outage: there is nothing to change on our side)
    false-alarm         an alert written by the harness in the lag rule's own words, with a made-up number (the ledger
                        consumer group 250 messages behind), given to Klaxon on a healthy stack under traffic: the right
                        answer is that nothing is wrong, and no action (Aravind's choice, 2026-11-03)
    missing-deploys     fault 1 with the deploy list withheld: every call to recent_deploys gets the error docker gives
                        when it does not answer, after the 15 s a real one waits, and never reaches docker. An outage
                        the harness fakes, said in the run's evidence; the stack is not touched. The right answer names
                        the cause and says the deploy history could not be checked (his choice, 2026-11-03)

A run is one case, one seed and one model, on a fresh stack (make reset, then make up), as week 4's runs were. The seed
is the traffic's: its amounts, its timing, which answers get lost. Klaxon gets the budgets of weeks 3 and 4 (10 steps,
8 tool calls), and is offered one write action, as in week 4. But its gate only plans what a diagnosis names
(klaxon.agent.gate.plan_only): nobody is asked, and nothing runs. So the evaluation counts the actions Klaxon would
take, and the ones code refuses to plan, without anyone's approval. The money is read at every diagnosis, whatever it
names: the damage at the alert and at the diagnosis say what investigating cost (known gap 6a). The fault is cleared at
the end, as in every fault run.

A suite is one model and a list of cases and seeds, run one after another, seed by seed: a suite stopped early has the
first seed of every case. It stops at the first run whose loop stopped in a way that says nothing of Klaxon's judgment
and would likely happen again (the model could not answer, a seat's limit for one; the tools could not start; a
crash), and at an interrupt. The runs left are said as not run, and nothing is run again on its own. Each run's evidence
goes into the suite's folder in evidence/week5/, as a fault run's does; the suite's own file says what was planned and
what came of each run, and the report is written there at the end.

The report is worked out from saved runs, offline, so a grader can change and every result be graded again without a
new run: per run, the verdict, the fix and the action, the gate, the times, the model calls, the cost, the damage; per
case and model, counts ("2 of 3") and medians, never a percentage of three runs. It reads week 3's and week 4's runs
too. The grading rule (klaxon.grading, rubric version 3) grades the six faults; for missing-deploys it grades the cause
only. Whether a false alarm was called false, and whether a diagnosis said what it could not check, wait for week 5's
grader (item 2).

Rehearsals (no model): a case's steps without the loop, as `make rehearse-fault` does for a fault. For the false alarm,
the quiet stack, the alert it would give, and the rules read for a minute more. For missing-deploys, fault 1's
rehearsal, then a probe of the tools through the case's own client: recent_deploys withheld, service_health answering.

Run: python -m klaxon.evaluation cases                                      calls nothing
     python -m klaxon.evaluation run --model ID [--cases A,B] [--seeds 7,8,9]  live, with Aravind's OK for each batch
     python -m klaxon.evaluation report FOLDER [FOLDER ...]                   offline
     python -m klaxon.evaluation rehearse CASE [--seed N]                      the stack, no model; his OK first
"""

from __future__ import annotations

import argparse
import json
import shlex
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from klaxon import alerts, fault_runs, faults, traffic
from klaxon.agent import catalog, gate, loop, verify
from klaxon.agent.claude_code import DEFAULT_MODEL
from klaxon.agent.loop import Alert, Budgets, RunResult
from klaxon.agent.mcp_client import START_TIMEOUT, McpClient, McpError, ToolClient, ToolResult, ToolSpec
from klaxon.agent.model import ModelError, read_records
from klaxon.agent.tools import DOCKER_TIMEOUT, ToolInputError, check_arguments, clean_text
from klaxon.checks import live
from klaxon.patient import stack as patient
from klaxon.patient.deploys import Deployer
from klaxon.patient.stack import REPO, StackError

FAULT, FALSE_ALARM, MISSING = "fault", "false alarm", "missing evidence"
ROLLBACK = catalog.ROLLBACK_DEPLOY.name
EVIDENCE = REPO / "evidence" / "week5"
SEEDS = (7, 8, 9)  # Aravind's choice (2026-11-03): 7 is the seed of every earlier run
STEPS, TOOL_CALLS = 10, 8  # weeks 3 and 4's budgets (make investigate-fault STEPS=10 TOOL_CALLS=8)
SUITE_FILE, REPORT_JSON, REPORT_MD, PROBE_FILE = "suite.json", "report.json", "report.md", "tools-probe.json"
FALSE_LAG = 250  # the false alarm's made-up lag, in messages: five times the rule's threshold
REHEARSAL_EVALUATIONS = 4  # a false alarm's rehearsal reads the rules this many times more: about a loop's minute
# What a docker-backed tool answers when docker does not (klaxon.agent.tools._docker_view's own words), and how long
# a real one takes to say it: docker's first read times out.
DOCKER_SILENT = f"docker did not answer within {DOCKER_TIMEOUT:.0f} s: is Docker Desktop running?"
OUTAGES = {"recent_deploys": (DOCKER_SILENT, DOCKER_TIMEOUT)}
# The loop's stops that say nothing of Klaxon's judgment: its run is "not run". The first three also stop a suite.
STOPS_A_SUITE = ("model_error", "tools_failed", "crashed")
NOT_RUN = (*STOPS_A_SUITE, "interrupted")
NOT_GRADED = "not graded yet: week 5's grader (item 2)"
CAUSE_ONLY = "whether a diagnosis said what it could not check is item 2's to grade"


@dataclass(frozen=True)
class Case:
    """One case of the evaluation, with its right answer, committed before any run on it."""

    name: str
    kind: str  # fault, false alarm, or missing evidence
    title: str
    right_answer: str  # what a right diagnosis says, in plain words
    right_action: tuple[str, str] | None  # the action and service a right diagnosis may name; None: no action fits
    fault: str | None = None  # the fault injected (klaxon.faults); a false alarm injects none
    withheld: tuple[str, ...] = ()  # tools whose every call gets the error their source gives when it does not answer

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "title": self.title,
            "right_answer": self.right_answer,
            "right_action": None if self.right_action is None else list(self.right_action),
            "fault": self.fault,
            "withheld": {name: OUTAGES[name][0] for name in self.withheld},
        }


def _fault_case(name: str, service: str | None) -> Case:
    fault = faults.FAULTS[name]
    action = None if service is None else (ROLLBACK, service)
    return Case(name, FAULT, fault.title, f"The fault's recorded cause: {fault.cause}.", action, fault=name)


CASES: dict[str, Case] = {
    case.name: case
    for case in (
        _fault_case("duplicate-delivery", faults.LEDGER),
        _fault_case("poison-message", faults.LEDGER),
        _fault_case("provider-outage", None),
        _fault_case("money-unit", faults.API),
        _fault_case("secret-rotation", faults.RECEIVER),
        _fault_case("pool-exhaustion", faults.API),
        Case(
            "false-alarm",
            FALSE_ALARM,
            "A false alarm: the ledger consumer group said to be 250 messages behind, on a healthy stack",
            "Nothing is wrong: the ledger consumer group is not behind (under traffic its lag stays near 0), so the"
            " alert is false. A right diagnosis says so, from a tool that read the lag, proposes no fix (kind none)"
            " and names no action.",
            None,
        ),
        Case(
            "missing-deploys",
            MISSING,
            "Payments counted twice in the books, with the deploy list withheld",
            "The ledger consumer books a second time every webhook the provider sends again: its dedupe is gone (fault"
            " 1's recorded cause, which a deploy brought). The deploy list could not be read, so a right diagnosis says"
            " the deploy history could not be checked, and claims no deploy it did not see. Rolling the ledger consumer"
            " back still fits the remedy: code plans a rollback from the deploy log, not from the tool.",
            (ROLLBACK, faults.LEDGER),
            fault="duplicate-delivery",
            withheld=("recent_deploys",),
        ),
    )
}


def get_case(name: str) -> Case:
    if name not in CASES:
        raise ValueError(f"no case {name!r}; the cases are: {', '.join(CASES)}")
    return CASES[name]


def false_alert(at_utc: str) -> Alert:
    """The false alarm's alert: in the lag rule's own words (klaxon.alerts writes them), with a made-up number."""
    alert = alerts.raise_alert(alerts.lags([({"consumer": "ledger"}, float(FALSE_LAG))]), at_utc)
    assert alert is not None  # a lag above the threshold always makes one
    return alert


class Withheld:
    """A ToolClient whose calls to the tools named never reach the tool server: each gets the error the tool gives
    when its source does not answer, after the time a real one takes. An outage the harness fakes for one case,
    said as such in the run's evidence. Every other call goes to the inner client, and so does a call whose arguments
    the tool would refuse first (the server's own error is the right one then)."""

    def __init__(
        self,
        inner: ToolClient,
        names: Iterable[str],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.inner = inner
        self.outages = {name: OUTAGES[name] for name in names}
        self.sleep = sleep
        self.specs: dict[str, ToolSpec] = {}

    @property
    def server_info(self) -> dict:
        return getattr(self.inner, "server_info", {})

    def open(self, timeout: float = START_TIMEOUT) -> list[ToolSpec]:
        listed = self.inner.open(timeout)
        self.specs = {spec.name: spec for spec in listed}
        return listed

    def call(self, name: str, arguments: Mapping[str, object], timeout: float) -> ToolResult:
        if name not in self.outages or name not in self.specs:
            return self.inner.call(name, arguments, timeout)
        try:
            check_arguments(self.specs[name].input_schema, arguments)
        except ToolInputError:  # the tool refuses these arguments before it reads its source: the server says why
            return self.inner.call(name, arguments, timeout)
        text, seconds = self.outages[name]
        self.sleep(min(seconds, timeout))
        return ToolResult(text, True)

    def close(self) -> None:
        self.inner.close()


def dry_gate(
    folder: Path,
    deployer: Deployer,
    damage: dict,
    read_money: Callable[[], verify.Reading] = lambda: verify.read(live.check_live, alerts.MONEY_SETTLE_SECONDS),
) -> fault_runs.Gated:
    """The gate of an evaluation run: the money is read at the diagnosis, whatever it names (damage["at_diagnosis"]),
    then code plans the action it names, and nobody is asked (gate.plan_only). Nothing runs, so there is nothing to
    verify or undo: those two parts refuse, and are never reached."""

    def act(alert: Alert, diagnosis: object, asking: Callable[[], None]) -> gate.Outcome:
        damage["at_diagnosis"] = read_money().to_dict()
        return gate.plan_only(folder, diagnosis, deployer)

    def nothing_ran(*args: object) -> object:
        raise RuntimeError("an evaluation run runs no action, so there is nothing to verify or undo")

    return fault_runs.Gated(read_money, act, lambda: None, nothing_ran, nothing_ran, deployer.log.records)


# The false alarm: no fault, and an alert written by the harness.


def false_alarm(
    case: Case,
    record: fault_runs.Record,
    stack: fault_runs.Stack,
    settings: traffic.Settings,
    investigate: Callable[[Alert], RunResult] | None,
    gated: fault_runs.Gated | None,
    warmup_seconds: float = fault_runs.WARMUP_SECONDS,
    every_seconds: float = alerts.EVERY_SECONDS,
) -> fault_runs.Record:
    """A false alarm, filling in record as it goes: traffic and no fault; the rules must see a quiet stack at two
    evaluations in a row, as before a fault; then the case's alert, written by the harness, is given to the loop as
    if the rules had raised it. Any action the diagnosis names is planned by code, and nobody is asked. Nothing is
    injected, so nothing is cleared; the rules are read once more after the loop, and the traffic always stops. With no
    investigate, a rehearsal: the alert it would give, and the rules read for about a minute more, with no model."""
    record.gated = gated is not None
    record.rehearsal = investigate is None
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
        for n in range(fault_runs.QUIET_EVALUATIONS):
            if n:
                stack.sleep(every_seconds)
            why = fault_runs.not_quiet(evaluate("before"))
            if why is not None:
                record.stopped = f"not quiet before the false alarm, so none was given: {why}"
                return record
        last = record.evaluations[-1]
        alert = false_alert(last["at_utc"])
        record.alert = asdict(alert)
        record.timings["alert_given_at"] = since()
        record.damage["at_alert"] = fault_runs.at_alert(last)
        if investigate is None:  # a rehearsal: what the rules see while a loop would investigate
            for _ in range(REHEARSAL_EVALUATIONS):
                stack.sleep(every_seconds)
                evaluate("during")
            record.stopped = "a rehearsal: the false alarm's alert was not given to any model"
            return record
        record.timings["loop_started_at"] = since()
        result = investigate(alert)
        record.timings["loop_ended_at"] = since()
        record.loop = fault_runs.summary(result)
        record.stopped = f"the loop stopped: {result.reason}"
        if gated is not None and result.diagnosis is not None:
            record.timings["gate_began_at"] = since()
            outcome = gated.act(alert, result.diagnosis, lambda: None)
            record.timings["gate_ended_at"] = since()
            record.gate = asdict(outcome) | {"decided": None}
        evaluate("after")  # once the loop is done: still quiet, so the alert was false
    finally:
        generator.close()
        generator.join()
        record.traffic = generator.stats.to_dict()
        record.provider_behavior = generator.behavior_note
    return record


def false_alarm_label(record: fault_runs.Record) -> str:
    """What a false alarm's evidence is, from what its run did: it never claims a step the run did not reach."""
    if record.traffic is None:
        return (
            "Real run on this Mac's Docker Compose stack, which stopped before its synthetic traffic began: no alert"
            f" was given and no model was called. The run stopped: {record.stopped}."
        )
    said = [
        "Real run on this Mac's Docker Compose stack, with synthetic traffic from klaxon.traffic: synthetic data, a"
        " simulated provider, no real money.",
        "No fault was injected: an evaluation case, a false alarm (week 5).",
    ]
    if record.alert is None:
        return " ".join([*said, f"No alert was given. The run stopped: {record.stopped}."])
    said.append(
        "Its alert was written by the harness in the lag rule's own words, with a made-up number: the rules did not"
        " raise it, and they saw a quiet stack right before it was given."
    )
    if record.rehearsal:
        said.append("A rehearsal: no model was called and nothing was offered; the rules were read for a minute more.")
    if "loop_started_at" in record.timings:
        if record.loop is None:
            said.append(
                f"Klaxon's loop started live on it, through Claude Code on Aravind's seat{fault_runs.EVALUATION_OFFERED},"
                f" and did not finish: {record.stopped}."
            )
        else:
            said.append(
                f"Klaxon's loop ran live on it, through Claude Code on Aravind's seat{fault_runs.EVALUATION_OFFERED}, and"
                f" stopped on {record.loop['reason']} after {record.loop['steps']} steps; any reply it got is a recorded"
                " response, not a simulation."
            )
    if record.gated:
        said += fault_runs._gated_label(record)
    return " ".join(said)


def label(record: fault_runs.Record) -> str:
    if record.case is not None and record.case["kind"] == FALSE_ALARM:
        return false_alarm_label(record)
    return fault_runs.label(record)


def render(record: fault_runs.Record) -> str:
    """A run of the evaluation, printed: a fault's run as klaxon.fault_runs prints it, a false alarm here."""
    if record.case is None or record.case["kind"] != FALSE_ALARM:
        return fault_runs.render(record)
    case = record.case
    lines = [
        f"Klaxon week 5: an evaluation run (case {case['name']}, seed {case['seed']}): a false alarm",
        label(record),
        "",
        f"The case: {record.fault['title']}",
        f"  right answer: {record.fault['right_answer']}",
        "",
        "The alert rules (t: when each evaluation began, from the start of the traffic; rates and p95 are over the"
        " last minute)",
        *(f"  {e['phase']:<6} t={e['began']:>6.1f} s  {fault_runs.brief(e)}" for e in record.evaluations),
    ]
    if record.alert is not None:
        lines += [
            "",
            f"Alert {record.alert['name']} (written by the harness, not raised by the rules), given at"
            f" {record.alert['fired_at']}: {record.alert['summary']}",
        ]
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
                f"  proposed fix (the model's words): {fix['kind']} {clean_text(fix['target'], 200)}:"
                f" {clean_text(fix['summary'])}",
            ]
            named = found.get("fix_action")
            if named is not None:
                lines.append(
                    f"  action it names: {clean_text(named['name'], 64)}"
                    f" {clean_text(json.dumps(named['arguments']), 300)} (an evaluation run: planned, never run)"
                )
    if record.gate is not None:
        lines += ["", f"The gate: {record.gate['result']}: {clean_text(record.gate['detail'], 500)}"]
    if record.damage.get("at_diagnosis"):
        entry = record.damage["at_diagnosis"]
        lines += ["", f"The money at the diagnosis: {entry['at_utc']}  {fault_runs.damage_line(entry)}"]
    lines += ["", f"Stopped: {record.stopped}"]
    return "\n".join(lines)


def save(folder: Path, record: fault_runs.Record, header: Mapping) -> None:
    """A run's evidence, written and printed, as a fault run's is."""
    if record.case is None or record.case["kind"] != FALSE_ALARM:
        fault_runs.save(folder, record, header)
        return
    out = {"label": label(record), **header, **asdict(record), "printed_output": render(record).splitlines()}
    (folder / fault_runs.RUN_FILE).write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(render(record))
    print(f"\nSaved in {clean_text(str(folder))}")


# One run, live.


def what(case: Case, seed: int) -> str:
    """What a run is, at the top of its loop's files (the model recording and the tool results)."""
    said = (
        f"One evaluation run of Klaxon (week 5): case {case.name}, seed {seed}. {case.title}. Claude on Aravind's seat,"
        " read-only tools on this Mac's stack, and one write action offered, which code plans and nobody is asked"
        " about: nothing runs."
    )
    if case.withheld:
        said += " " + fault_runs.withheld_sentence({name: OUTAGES[name][0] for name in case.withheld})
    return said


def run_case(
    case: Case,
    seed: int,
    model: str,
    budgets: Budgets,
    rate: float,
    parent: Path,
    binary: Path,
) -> dict:
    """One case with one seed and one model, live, on the stack as it is (the suite has made it fresh). Its evidence
    goes into a folder of its own in parent. Returns what came of it, for the suite's own file."""
    settings = traffic.Settings(rate=rate, seed=seed)
    argv = ["run", "--model", model, "--cases", case.name, "--seeds", str(seed)]
    argv += ["--steps", str(budgets.steps), "--tool-calls", str(budgets.tool_calls), "--rate", f"{rate:g}"]
    command = shlex.join(["python", "-m", "klaxon.evaluation", *argv])
    parts = fault_runs.Stack(faults.LocalStack(), alerts.Alerting())
    header = {"case": case.name, "seed": seed, "model": model, "fault_name": case.fault} | fault_runs._provenance(
        command
    )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    folder = loop._new_folder(parent, f"{stamp}_{case.name}_seed{seed}")
    described = case.describe() | {"seed": seed, "model": model}
    fault = None if case.fault is None else faults.get(case.fault)
    record = fault_runs.Record(
        described if fault is None else fault.describe(), asdict(settings), asdict(budgets), case=described
    )
    gated = dry_gate(folder, parts.local.deployer, record.damage)
    print(f"The run's folder: {clean_text(str(folder))}", flush=True)

    def investigate(alert: Alert) -> RunResult:
        run_header = header | {"what": what(case, seed), "alert": asdict(alert), "budgets": asdict(budgets)}
        client = Withheld(McpClient(), case.withheld) if case.withheld else None
        return loop.investigate_live(
            alert, budgets, folder, run_header, model, binary, actions=fault_runs.OFFERED, tool_client=client
        )

    interrupted = False
    try:
        with fault_runs.stops_as_interrupts():
            if fault is None:
                false_alarm(case, record, parts, settings, investigate, gated)
            else:
                fault_runs.run(fault, record, parts, settings, investigate, gated=gated)
    except KeyboardInterrupt as exc:
        record.stopped, interrupted = "interrupted" + (f" ({exc})" if str(exc) else ""), True
    except Exception as exc:  # the fault was cleared and the traffic stopped, unless this is where it failed
        record.stopped = f"error: {type(exc).__name__}: {clean_text(str(exc), 300)}"
    save(folder, record, header)
    return {
        "case": case.name,
        "seed": seed,
        "folder": folder.name,
        "stopped": record.stopped,
        "loop": None if record.loop is None else record.loop["reason"],
        "interrupted": interrupted,
    }


# A suite: one model, its cases and seeds, one run after another.


def plan(cases: Sequence[Case], seeds: Sequence[int]) -> list[tuple[Case, int]]:
    """Seed by seed: every case with the first seed, then every case with the next."""
    return [(case, seed) for seed in seeds for case in cases]


def ends_the_suite(outcome: Mapping) -> str | None:
    """Why the suite must stop after this run, or None: an interrupt, an error outside the loop, or a loop that
    stopped in a way that would likely happen again (the model could not answer; the tools could not start; a
    crash). Never a run Klaxon got wrong, nor one whose fault raised no alert."""
    if outcome["interrupted"]:
        return "it was interrupted"
    if outcome["stopped"].startswith("error:"):
        return f"it stopped on an {outcome['stopped']}"
    if outcome["loop"] in STOPS_A_SUITE:
        return f"its loop stopped on {outcome['loop']}"
    return None


def run_suite(
    planned: Sequence[tuple[Case, int]],
    suite: dict,
    write: Callable[[dict], None],
    fresh_stack: Callable[[], None],
    run_one: Callable[[Case, int], dict],
) -> dict:
    """The planned runs, one after another, each on a fresh stack, with the suite's file written after each. It stops
    at the first run ends_the_suite names, when the stack cannot be made fresh, when a run cannot even start, and at
    an interrupt: the runs left are said as not run, and nothing is run again on its own."""
    suite["runs"], suite["not_run"], suite["stopped"] = [], [], None
    write(suite)
    for n, (case, seed) in enumerate(planned):
        left = [{"case": c.name, "seed": s} for c, s in planned[n:]]
        try:
            fresh_stack()
        except (StackError, OSError) as exc:
            suite["stopped"] = f"the stack could not be reset and started: {clean_text(str(exc), 300)}"
            suite["not_run"] = left
            break
        except KeyboardInterrupt:
            suite["stopped"], suite["not_run"] = "interrupted while the stack was made fresh", left
            break
        try:
            outcome = run_one(case, seed)
        except Exception as exc:  # before its run could begin (docker gone, say): nothing of it to keep
            suite["stopped"] = (
                f"{case.name}, seed {seed}, could not start: {type(exc).__name__}: {clean_text(str(exc), 300)}"
            )
            suite["not_run"] = left
            break
        except KeyboardInterrupt:
            suite["stopped"], suite["not_run"] = f"interrupted as {case.name}, seed {seed}, was starting", left
            break
        suite["runs"].append(outcome)
        why = ends_the_suite(outcome)
        if why is not None:
            suite["stopped"] = f"after {outcome['folder']}, because {why}"
            suite["not_run"] = [{"case": c.name, "seed": s} for c, s in planned[n + 1 :]]
            write(suite)
            break
        write(suite)
    write(suite)
    return suite


def fresh_stack() -> None:
    """make reset, then make up: empty books, and the good settings pinned (klaxon.patient.stack)."""
    patient.reset()
    patient.up()


# The report: from saved runs, offline.


def _mode(saved: Mapping) -> str:
    if saved.get("case") is not None:
        return "evaluation"
    if saved.get("rehearsal"):
        return "rehearsal"
    return "gated, week 4" if saved.get("gated") else "diagnosis only, week 3"


def usage(folder: Path) -> dict:
    """The run's model calls, from its recording: how many, how many accepted, which models answered, and Claude
    Code's cost estimate at API prices (the seat is not billed per call)."""
    path = folder / loop.RECORDING
    responses = [r for r in read_records(path) if r.get("kind") == "response"] if path.exists() else []
    costs = [r.get("cost_usd") for r in responses]
    return {
        "calls": len(responses),
        "accepted": sum(1 for r in responses if r.get("accepted") is True),
        "models": sorted({r["model"] for r in responses if isinstance(r.get("model"), str)}),
        "cost_usd": round(sum(c for c in costs if isinstance(c, int | float) and not isinstance(c, bool)), 4),
        "calls_without_cost": sum(1 for c in costs if not isinstance(c, int | float) or isinstance(c, bool)),
    }


def _findings(entry: Mapping | None) -> dict | None:
    """A damage reading in numbers: its outcome, and its findings per invariant."""
    if not entry:
        return None
    if "money_check" in entry:  # the alert's own evaluation of the rules
        money = entry["money_check"]
        return {"outcome": money.get("outcome"), "findings": dict(money.get("failed") or {})}
    counted = Counter(str(finding.get("invariant")) for finding in entry.get("findings") or [])
    return {"outcome": entry.get("outcome"), "findings": dict(counted)}


def not_run(saved: Mapping) -> str | None:
    """Why this run says nothing of Klaxon's judgment, or None: its loop never ran (the stack was not quiet, no alert
    fired, an error), or stopped in a way that is not Klaxon's (the model could not answer, the tools could not start,
    an interrupt, a crash)."""
    found = saved.get("loop")
    if found is None:
        return f"the loop never ran: {saved.get('stopped')}"
    if found.get("reason") in NOT_RUN:
        return f"the loop stopped on {found['reason']}: {clean_text(str(found.get('detail')), 200)}"
    return None


def run_row(folder: Path) -> dict:
    """One saved run, in the numbers the report counts. Reads its run.json and model recording; calls nothing."""
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    if not isinstance(saved, dict):
        raise ValueError(f"{folder.name}'s run.json is not an object")
    case = saved.get("case") or {}
    name = case.get("name") or saved["fault"]["name"]
    known = CASES.get(name)
    found = saved.get("loop") or {}
    diagnosis = found.get("diagnosis")
    comparison = saved.get("comparison") or {}
    graded = comparison.get("verdict") not in (None, "no diagnosis")
    offered = bool(saved.get("gated"))
    named = None if diagnosis is None else diagnosis.get("fix_action")
    named_pair = None if named is None else [named.get("name"), (named.get("arguments") or {}).get("service")]
    right = None if known is None or known.right_action is None else list(known.right_action)
    timings = saved.get("timings") or {}
    alert_after = None
    if "alert_evaluation_began" in timings and "injected_at" in timings:
        alert_after = round(timings["alert_evaluation_began"] - timings["injected_at"], 1)
    used = usage(folder)
    damage = saved.get("damage") or {}
    return {
        "folder": folder.name,
        "case": name,
        "kind": case.get("kind") or (known.kind if known else FAULT),
        "mode": _mode(saved),
        "seed": (saved.get("traffic_settings") or {}).get("seed"),
        "model": case.get("model") or (used["models"][0] if len(used["models"]) == 1 else None),
        "answered_by": used["models"],
        "not_run": not_run(saved),
        "loop": found.get("reason"),
        "diagnosed": diagnosis is not None,
        "confidence": None if diagnosis is None else diagnosis.get("confidence"),
        "verdict": comparison["verdict"] if graded else None,
        "rubric": (comparison.get("rubric") or {}).get("version") if graded else None,
        "fix_fits": comparison.get("fix_fits_the_remedy") if graded else None,
        "offered": offered,
        "named_action": named_pair,
        "right_action": right,
        # Offered an action, and diagnosed: right when it named the action that fits, or none where none fits.
        "action_right": (named_pair == right) if offered and diagnosis is not None else None,
        "action_where_none_fits": offered and named_pair is not None and right is None,
        "gate": (saved.get("gate") or {}).get("result"),
        "alert_after_fault_seconds": alert_after,
        "diagnosis_seconds": None if found.get("seconds") is None else round(found["seconds"], 1),
        "steps": found.get("steps"),
        "tool_calls": found.get("tool_calls"),
        "calls": used["calls"],
        "accepted_calls": used["accepted"],
        "cost_usd": used["cost_usd"],
        "calls_without_cost": used["calls_without_cost"],
        "damage_at_alert": _findings(damage.get("at_alert")),
        "damage_at_diagnosis": _findings(damage.get("at_diagnosis") or damage.get("at_request")),
    }


def run_folders(paths: Iterable[Path]) -> tuple[list[Path], list[str]]:
    """The run folders in these paths, in order: a run's own folder, or a folder of them (a suite's, a week's).
    Rehearsals are left out, and said: no model ran in them."""
    found, left_out = [], []
    for path in paths:
        folders = [path] if (path / fault_runs.RUN_FILE).is_file() else sorted(p for p in path.iterdir() if p.is_dir())
        for folder in folders:
            if not (folder / fault_runs.RUN_FILE).is_file():
                continue
            if "_rehearsal_" in folder.name:
                left_out.append(f"{folder.name}: a rehearsal, with no model")
                continue
            found.append(folder)
    return found, left_out


def _median(values: Iterable[object]) -> float | None:
    numbers = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    return None if not numbers else round(statistics.median(numbers), 1)


def summarize(rows: Sequence[Mapping]) -> list[dict]:
    """Per model and case: what ran, and counts of what came of it. A run that says nothing of Klaxon's judgment
    (not_run) is counted apart, never as wrong."""
    groups: dict[tuple, list[Mapping]] = {}
    for row in rows:
        groups.setdefault((row["model"] or "unknown", row["case"]), []).append(row)
    out = []
    for (model, name), group in groups.items():
        ran = [row for row in group if row["not_run"] is None]
        graded = [row for row in ran if row["verdict"] is not None]
        acted = [row for row in ran if row["action_right"] is not None]
        out.append(
            {
                "model": model,
                "case": name,
                "kind": group[0]["kind"],
                "modes": sorted({row["mode"] for row in group}),
                "runs": len(group),
                "not_run": len(group) - len(ran),
                "ran": len(ran),
                "diagnosed": sum(1 for row in ran if row["diagnosed"]),
                "graded": len(graded),
                "names_the_cause": sum(1 for row in graded if row["verdict"] == "names the cause"),
                "partly": sum(1 for row in graded if row["verdict"] == "partly"),
                "misses_the_cause": sum(1 for row in graded if row["verdict"] == "misses the cause"),
                "fix_fits": sum(1 for row in graded if row["fix_fits"] is True),
                "action_judged": len(acted),
                "action_right": sum(1 for row in acted if row["action_right"]),
                "action_where_none_fits": sum(1 for row in ran if row["action_where_none_fits"]),
                "refused_by_code": sum(1 for row in ran if row["gate"] == "refused"),
                "median_alert_after_fault_seconds": _median(row["alert_after_fault_seconds"] for row in ran),
                "median_diagnosis_seconds": _median(row["diagnosis_seconds"] for row in ran),
                "median_steps": _median(row["steps"] for row in ran),
                "median_calls": _median(row["calls"] for row in ran),
                "calls": sum(row["calls"] for row in group),
                "cost_usd": round(sum(row["cost_usd"] for row in group), 4),
            }
        )
    order = list(CASES)
    out.sort(key=lambda e: (e["model"], order.index(e["case"]) if e["case"] in order else len(order), e["case"]))
    return out


def shown(path: Path) -> str:
    """A folder as evidence may name it: from the repo's root, or with this Mac's home folder written as ~."""
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return clean_text(str(path))


def report(paths: Sequence[Path]) -> dict:
    folders, left_out = run_folders(paths)
    rows = [run_row(folder) for folder in folders]
    return {
        "label": (
            "Worked out offline from saved runs by klaxon.evaluation: no model was called and nothing was run. The"
            " verdicts are the grading rule's (klaxon.grading), not a person's or a model's. The costs are Claude"
            " Code's estimates at API prices; the seat is not billed per call."
        ),
        "made_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "from": [shown(path) for path in paths],
        "left_out": left_out,
        "runs": rows,
        "summary": summarize(rows),
    }


def _of(count: int, total: int) -> str:
    return "-" if total == 0 else f"{count} of {total}"


def _number(value: object, unit: str = "") -> str:
    return "-" if value is None else f"{value:g}{unit}"


def _cell(text: object) -> str:
    return clean_text(str(text), 300).replace("|", "/")


def markdown(found: Mapping) -> str:
    """The report as a page: per model, a table of the cases; then every run, and the runs that did not run."""
    lines = ["# Klaxon evaluation report", "", found["label"], ""]
    lines.append(f"Made {found['made_at_utc']} from {len(found['runs'])} runs in: {', '.join(found['from'])}.")
    if found["left_out"]:
        lines.append(f"Left out: {'; '.join(found['left_out'])}.")
    for model in sorted({entry["model"] for entry in found["summary"]}):
        lines += [
            "",
            f"## {model}",
            "",
            "| Case | Runs | Named the cause | Fix fits | Action right | Named where none fits | Refused by code"
            " | Median s, fault to alert | Median s to diagnose | Median calls | Cost |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        entries = [e for e in found["summary"] if e["model"] == model]
        for entry in entries:
            graded = entry["graded"]
            named = _of(entry["names_the_cause"], graded) if graded else NOT_GRADED.split(":")[0]
            lines.append(
                f"| {_cell(entry['case'])} | {entry['ran']} ran, {entry['not_run']} not run | {named} |"
                f" {_of(entry['fix_fits'], graded)} | {_of(entry['action_right'], entry['action_judged'])} |"
                f" {entry['action_where_none_fits']} | {entry['refused_by_code']} |"
                f" {_number(entry['median_alert_after_fault_seconds'])} | {_number(entry['median_diagnosis_seconds'])} |"
                f" {_number(entry['median_calls'])} | ${entry['cost_usd']:.2f} |"
            )
        runs, missing = sum(e["runs"] for e in entries), sum(e["not_run"] for e in entries)
        calls, cost = sum(e["calls"] for e in entries), sum(e["cost_usd"] for e in entries)
        lines += ["", f"In all: {runs} runs ({missing} not run), {calls} model calls, ${cost:.2f}."]
    lines += [
        "",
        "Named the cause: the rule's verdict, of the runs it graded: the six faults, and missing-deploys on its cause"
        f" alone ({CAUSE_ONLY}). The false alarm is {NOT_GRADED}. A run that did not run counts only in the first"
        " column, and in the totals.",
        "",
        "## Each run",
        "",
        "| Run | Case | Seed | Mode | Verdict | Action named | Gate | s to diagnose | Calls | Cost |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in found["runs"]:
        verdict = row["verdict"] or ("not run" if row["not_run"] else "not graded")
        action = "-" if row["named_action"] is None else " ".join(str(part) for part in row["named_action"])
        lines.append(
            f"| {_cell(row['folder'])} | {_cell(row['case'])} | {row['seed']} | {row['mode']} | {verdict} |"
            f" {_cell(action)} | {row['gate'] or '-'} | {_number(row['diagnosis_seconds'])} | {row['calls']} |"
            f" ${row['cost_usd']:.2f} |"
        )
    missing = [row for row in found["runs"] if row["not_run"]]
    if missing:
        lines += ["", "## Not run", ""]
        lines += [f"- {_cell(row['folder'])}: {_cell(row['not_run'])}" for row in missing]
    return "\n".join(lines) + "\n"


def write_report(folder: Path, found: Mapping) -> None:
    (folder / REPORT_JSON).write_text(json.dumps(found, indent=2, default=str) + "\n")
    (folder / REPORT_MD).write_text(markdown(found))


# Rehearsals: a case's steps, the stack and the rules, and no model.


def probe_tools(names: Sequence[str], client: ToolClient, timeout: float = 60.0) -> list[dict]:
    """Each tool named called once, with no arguments, through client: what the model would have seen."""
    out = []
    client.open()
    try:
        for name in names:
            result = client.call(name, {}, timeout)
            out.append({"tool": name, "is_error": result.is_error, "text": clean_text(result.text, 2000)})
    finally:
        client.close()
    return out


def rehearse(case: Case, settings: traffic.Settings, folder: Path, header: Mapping) -> int:
    """A case under traffic, with the rules and no model; its evidence saved in folder."""
    parts = fault_runs.Stack(faults.LocalStack(), alerts.Alerting())
    described = case.describe() | {"seed": settings.seed, "model": None}
    fault = None if case.fault is None else faults.get(case.fault)
    record = fault_runs.Record(described if fault is None else fault.describe(), asdict(settings), {}, case=described)
    code = 0
    try:
        with fault_runs.stops_as_interrupts():
            if fault is None:
                false_alarm(case, record, parts, settings, None, None)
            else:
                fault_runs.run(fault, record, parts, settings, None)
    except KeyboardInterrupt as exc:
        record.stopped, code = "interrupted" + (f" ({exc})" if str(exc) else ""), 130
    except Exception as exc:  # the fault was cleared and the traffic stopped, unless this is where it failed
        record.stopped, code = f"error: {type(exc).__name__}: {clean_text(str(exc), 300)}", 2
    save(folder, record, header)
    if case.withheld and not code:
        probed = {
            "label": (
                "A probe of Klaxon's tools through this case's own client, after the rehearsal, with no model: each"
                " tool called once with no arguments. The withheld ones never reach their source (klaxon.evaluation)."
            ),
            "results": probe_tools([*case.withheld, "service_health"], Withheld(McpClient(), case.withheld)),
        }
        (folder / PROBE_FILE).write_text(json.dumps(probed, indent=2) + "\n")
        for result in probed["results"]:
            print(f"{result['tool']}: {'error' if result['is_error'] else 'answered'}: {result['text'][:200]}")
    if code:
        return code
    return 0 if record.alert is not None else 1


# The command line.


def _names(text: str) -> list[Case]:
    if text == "all":
        return list(CASES.values())
    return [get_case(name.strip()) for name in text.split(",") if name.strip()]


def _seeds(text: str) -> list[int]:
    seeds = [int(seed) for seed in text.split(",") if seed.strip()]
    if not seeds or len(set(seeds)) != len(seeds) or any(seed < 0 for seed in seeds):
        raise ValueError("--seeds is a list of different whole numbers, such as 7,8,9")
    return seeds


def _cases() -> int:
    for case in CASES.values():
        action = "none" if case.right_action is None else " ".join(case.right_action)
        print(f"{case.name} ({case.kind}): {case.title}")
        print(f"  right answer: {case.right_answer}")
        print(f"  action that fits: {action}")
        if case.withheld:
            print(f"  withheld: {', '.join(case.withheld)}: each call gets {DOCKER_SILENT!r}")
        print()
    return 0


def _run(args: argparse.Namespace) -> int:
    try:  # before any folder is made: a suite that cannot start leaves nothing behind, and touches nothing
        cases, seeds = _names(args.cases), _seeds(args.seeds)
        budgets = Budgets(steps=args.steps, tool_calls=args.tool_calls)
        traffic.Settings(rate=args.rate, seed=seeds[0])
        binary = fault_runs.preflight(args.model)
    except (ValueError, ModelError, McpError, loop.ToolsRefused, StackError) as exc:
        print(f"Not started, and nothing was touched: {clean_text(str(exc), 300)}", file=sys.stderr)
        return 2
    planned = plan(cases, seeds)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    folder = loop._new_folder(args.evidence_dir, f"{stamp}_suite_{args.model}")
    suite = {
        "label": (
            "A suite of Klaxon's evaluation runs (week 5): one model, its cases and seeds, each run on a fresh stack"
            " (make reset, then make up), live through Claude Code on Aravind's seat. In every run code planned any"
            " action the diagnosis named, nobody was asked, and nothing ran."
        ),
        "model": args.model,
        "budgets": asdict(budgets),
        "rate": args.rate,
        "planned": [{"case": case.name, "seed": seed} for case, seed in planned],
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    print(f"The suite's folder: {clean_text(str(folder))}; {len(planned)} runs planned", flush=True)

    def write(value: dict) -> None:
        (folder / SUITE_FILE).write_text(json.dumps(value, indent=2, default=str) + "\n")

    def run_one(case: Case, seed: int) -> dict:
        print(f"\n== {case.name}, seed {seed} ==", flush=True)
        return run_case(case, seed, args.model, budgets, args.rate, folder, binary)

    with fault_runs.stops_as_interrupts():  # a stop signal between runs stops the suite as Ctrl-C does
        done = run_suite(planned, suite, write, fresh_stack, run_one)
    found = report([folder])
    write_report(folder, found)
    print(f"\n{markdown(found)}")
    if done["stopped"]:
        print(f"The suite stopped {done['stopped']}; not run: {len(done['not_run'])}.", file=sys.stderr)
    if any(run["interrupted"] for run in done["runs"]) or (done["stopped"] or "").startswith("interrupted"):
        return 130
    return 1 if done["stopped"] else 0


def _report(args: argparse.Namespace) -> int:
    try:
        found = report(args.folders)
        if args.write is not None:
            write_report(args.write, found)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Cannot make the report: {type(exc).__name__}: {clean_text(str(exc), 300)}", file=sys.stderr)
        return 2
    print(markdown(found))
    return 0


def _rehearse(args: argparse.Namespace, argv: Sequence[str]) -> int:
    try:
        case = get_case(args.case)
        settings = traffic.Settings(rate=args.rate, seed=args.seed)
        command = shlex.join(["python", "-m", "klaxon.evaluation", *argv])
        header = {"case": case.name, "seed": args.seed, "fault_name": case.fault} | fault_runs._provenance(command)
    except (ValueError, StackError) as exc:
        print(f"Not started, and nothing was touched: {clean_text(str(exc), 300)}", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    folder = loop._new_folder(args.evidence_dir, f"{stamp}_rehearsal_{case.name}")
    print(f"The rehearsal's folder: {clean_text(str(folder))}", flush=True)
    return rehearse(case, settings, folder, header)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="python -m klaxon.evaluation", description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("cases", help="the cases, each with its right answer and the action that fits; calls nothing")
    live_ = actions.add_parser("run", help="a suite of live runs, each on a fresh stack; Claude on Aravind's seat")
    live_.add_argument("--model", default=DEFAULT_MODEL)
    live_.add_argument("--cases", default="all", help="all, or names separated by commas")
    live_.add_argument("--seeds", default=",".join(str(seed) for seed in SEEDS))
    live_.add_argument("--steps", type=int, default=STEPS)
    live_.add_argument("--tool-calls", type=int, default=TOOL_CALLS)
    again = actions.add_parser("report", help="the report, from saved runs; offline")
    again.add_argument("folders", type=Path, nargs="+", help="run folders, or folders of them (a suite's, a week's)")
    again.add_argument("--write", type=Path, help="also write report.json and report.md into this folder")
    rehearsal = actions.add_parser("rehearse", help="one case under traffic, with the rules and no model")
    rehearsal.add_argument("case", choices=list(CASES))
    rehearsal.add_argument("--seed", type=int, default=SEEDS[0])
    for sub in (live_, rehearsal):
        sub.add_argument("--rate", type=float, default=3.0, help="new payments per second, on average")
        sub.add_argument("--evidence-dir", type=Path, default=EVIDENCE)
    args = parser.parse_args(argv)
    if args.action == "cases":
        return _cases()
    if args.action == "report":
        return _report(args)
    if args.action == "rehearse":
        return _rehearse(args, argv)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
