"""The panel's data: each saved run, read from the files it left in evidence/, as plain dicts. It only reads.

Nothing here calls a model, a tool or the stack, and nothing writes. A run is found by its folder's name in an index
made from evidence/, never by a path from a request. What a run's files say is untrusted text: the pages escape it.
The numbers come from the code behind the make targets (klaxon.evaluation's rows and report), so they are theirs.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from klaxon import evaluation, fault_runs
from klaxon.agent import gate, loop, postmortem, postmortem_review
from klaxon.agent.model import read_records
from klaxon.patient.stack import REPO

EVIDENCE = REPO / "evidence"
DEPTH = 4  # evidence/<week>/[kind/][<suite>/]<run>: the deepest a run sits
GRADERS = ("verdict", "rubric", "fix_fits", "judge", "judge_fix_fits", "judge_why")
PLATFORM_NAMES = {"compose": "Docker Compose", "kind": "kind (Kubernetes in Docker)"}
NO_RUN_FILE = f"it holds no {fault_runs.RUN_FILE}, only its trace and recordings, so make eval-report leaves it out"
PHASES = ("Alert", "Investigate", "Diagnose", "Approve", "Act", "Verify", "Postmortem")
# The make eval-report folders the evaluation page offers: each week's, and kind's apart, as the docs name them.
REPORT_FOLDERS = ("week3", "week4", "week5", "week6", "week6/kind")
# The live view (week 7, item 2): a run with no run.json yet, whose folder changed this recently, is still going.
LIVE_MINUTES = 30
# The gate's events that say how an approved action ended (klaxon.agent.gate.log): till one comes, the page follows.
SETTLED = ("done", "failed", "not run", "stopped", "refused", "decision not taken", "approval refused")
GATE_DEMO = "klaxon-gate-demo-"  # the folder make gate-demo makes (klaxon.demo.gate_demo), outside evidence/
SIMULATION = (
    "A simulation (make gate-demo): a made-up stack and a made-up diagnosis, to try the gate. No model was called, and"
    " nothing real runs."
)


def index(root: Path = EVIDENCE, also: Iterable[Path] = ()) -> dict[str, Path]:
    """Every saved run's folder, by its name, newest first: a folder that holds a run.json, or a trace with no run.json
    (week 3's first live run, or a run still going). Names start with the time the run began, so each is one run's; a
    name seen twice keeps its first folder. Also the folders named on the panel's own command line (make panel
    ALSO=), such as make gate-demo's: never a folder a request names."""
    root = Path(root)
    found: dict[str, Path] = {}
    for folder in also:
        if Path(folder).is_dir():
            found.setdefault(Path(folder).name, Path(folder))
    if not root.is_dir():
        return dict(sorted(found.items(), reverse=True))
    for top, dirs, files in os.walk(root):
        here = Path(top)
        dirs.sort()
        if here != root and (fault_runs.RUN_FILE in files or loop.TRACE in files):
            found.setdefault(here.name, here)
            dirs[:] = []  # nothing inside a run's folder is a run
        elif len(here.relative_to(root).parts) >= DEPTH:
            dirs[:] = []
    return dict(sorted(found.items(), reverse=True))


def where(folder: Path, root: Path = EVIDENCE) -> str:
    """The run's folder from evidence/, as the make commands name it."""
    try:
        return str(Path(folder).relative_to(root))
    except ValueError:
        return Path(folder).name


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def changed_at(folder: Path) -> float:
    """When anything in the run's folder last changed: its newest file's time (0 when it holds none)."""
    return max((entry.stat().st_mtime for entry in os.scandir(folder) if entry.is_file()), default=0.0)


def version(folder: Path) -> str:
    """A fingerprint of the run's folder as it is now: each file's name, size and time. The live view compares two."""
    entries = sorted((e.name, e.stat().st_size, e.stat().st_mtime_ns) for e in os.scandir(folder) if e.is_file())
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()[:16]


def in_progress(folder: Path, now: float) -> bool:
    """A run still going: no run.json yet, and something in its folder changed in the last LIVE_MINUTES."""
    return not (folder / fault_runs.RUN_FILE).is_file() and now - changed_at(folder) < LIVE_MINUTES * 60


def _seconds(stamp: object) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def waiting(request: Mapping | None, decision: Mapping | None, now: float) -> float | None:
    """The seconds left on a request nobody has answered, or None: answered, expired, or no request."""
    if not request or decision:
        return None
    expires = _seconds(request.get("expires_at"))
    return None if expires is None or expires <= now else expires - now


def row(folder: Path, root: Path = EVIDENCE) -> dict:
    """One run as the runs list shows it: klaxon.evaluation's row of numbers, with where it ran and its alert. A run
    whose files cannot be read is said, never fatal to the page."""
    base = {"folder": folder.name, "where": where(folder, root), "problem": None}
    if not (folder / fault_runs.RUN_FILE).is_file():
        if folder.name.startswith(GATE_DEMO):
            return base | {"note": "a simulation: make gate-demo"}
        if in_progress(folder, time.time()):
            return base | {"note": "in progress", "live": True}
        return base | {"problem": NO_RUN_FILE}
    try:
        saved = _json(folder / fault_runs.RUN_FILE)
        if not isinstance(saved, dict):
            raise ValueError("run.json is not an object")
        found = evaluation.run_row(folder)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return base | {"problem": f"its files cannot be read ({type(exc).__name__})"}
    return (
        base
        | found
        | {
            "platform": saved.get("platform") or "compose",
            "alert": (saved.get("alert") or {}).get("name"),
            "ran_at": saved.get("ran_at_utc"),
            "rehearsal": bool(saved.get("rehearsal")),
            "gated": bool(saved.get("gated")),
        }
    )


def held(found: Mapping, show: bool) -> dict:
    """A run's graders as the panel shows them. On a run Aravind has not labeled, neither grader's verdict shows
    unless asked: label-pick saves no list of the runs it picked, so every run he has not labeled might be one, and his
    label must stay blind (make eval-report holds back the judge's; make calibration-report both)."""
    out = dict(found)
    graded = any(out.get(key) is not None for key in GRADERS)
    if show or out.get("aravind") is not None or not graded:
        return out | {"held_back": False}
    return out | dict.fromkeys(GRADERS) | {"graders_disagree": False, "held_back": True}


def rows(root: Path = EVIDENCE, show: bool = False, also: Iterable[Path] = ()) -> list[dict]:
    """Every saved run, newest first, graders held back as held() says."""
    return [held(row(folder, root), show) for folder in index(root, also).values()]


def _records(folder: Path, name: str, problems: list[str]) -> list[dict]:
    path = folder / name
    if not path.is_file():
        return []
    try:
        return read_records(path)
    except (OSError, ValueError) as exc:
        problems.append(f"{name} cannot be read ({type(exc).__name__})")
        return []


def _object(folder: Path, name: str, problems: list[str]) -> dict | None:
    path = folder / name
    if not path.is_file():
        return None
    try:
        found = _json(path)
    except (OSError, ValueError) as exc:
        problems.append(f"{name} cannot be read ({type(exc).__name__})")
        return None
    if not isinstance(found, dict):
        problems.append(f"{name} is not an object")
        return None
    return found


def _text(folder: Path, name: str, problems: list[str]) -> str | None:
    path = folder / name
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        problems.append(f"{name} cannot be read ({type(exc).__name__})")
        return None


def _map(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def steps(trace: Sequence[Mapping], tools: Sequence[Mapping], replies: Sequence[Mapping]) -> list[dict]:
    """The loop's steps from its trace, each with the tool result it got (the tool results file, in order) and the
    reply it acted on (the recording, by the time the reply was received)."""
    pending = [record for record in tools if record.get("kind") == "tool_call"]
    by_time = {r.get("received_at"): r for r in replies if r.get("kind") == "response" and r.get("received_at")}
    out = []
    for record in trace:
        if record.get("kind") != "step":
            continue
        proposed, ran, back = _map(record.get("proposed")), _map(record.get("ran")), _map(record.get("came_back"))
        result = None
        if ran and pending and pending[0].get("tool") == ran.get("tool"):
            result = pending.pop(0)
        reply = by_time.get(_map(record.get("reply")).get("received_at"))
        out.append(
            {
                "step": record.get("step"),
                "at": record.get("at"),
                "t": record.get("t"),
                "action": proposed.get("action"),
                "why": proposed.get("why"),
                "allowed": record.get("allowed"),
                "decision": record.get("why"),
                "call_id": ran.get("call_id"),
                "tool": ran.get("tool"),
                "arguments": ran.get("arguments") if ran else None,
                "is_error": back.get("is_error"),
                "result": None if result is None else result.get("text"),
                "reply": None if reply is None else reply.get("text"),
                "step_seconds": record.get("step_seconds"),
                "tool_seconds": record.get("tool_seconds"),
            }
        )
    return out


def signals(saved: Mapping) -> dict | None:
    """The alert rules' own reading when the alert fired: the first evaluation that raised it."""
    for found in saved.get("evaluations") or []:
        if isinstance(found, Mapping) and found.get("alert"):
            return {
                "at_utc": found.get("at_utc"),
                "phase": found.get("phase"),
                "checks": list(found.get("checks") or []),
            }
    return None


def damage(saved: Mapping) -> list[dict]:
    """The money check's findings per invariant at each reading the run saved, as make eval-report counts them."""
    found = _map(saved.get("damage"))
    names = (
        ("at_alert", "at the alert"),
        ("at_diagnosis", "at the diagnosis"),
        ("at_request", "at the request"),
        ("at_action", "at the action"),
        ("after_action", "after the action"),
    )
    out = []
    for key, said in names:
        reading = evaluation._findings(found.get(key))
        if reading is not None:
            out.append({"when": said, "outcome": reading["outcome"], "findings": reading["findings"]})
    return out


def phases(
    saved: Mapping, alert: Mapping, found: Mapping, has_postmortem: bool, live: Mapping | None = None
) -> list[dict]:
    """The run's path, alert to postmortem: each phase done, not done (and why), going on now, or not reached. The
    alert and the loop's end come from run.json, or from the trace and the gate's files while a run has none (a run
    still going, or week 3's first live run)."""
    live = live or {}
    going, request, events = bool(live.get("going")), _map(live.get("request")), list(live.get("events") or [])
    gated = bool(saved.get("gated")) or bool(request)
    diagnosis, asked = _map(found.get("diagnosis")), _map(saved.get("gate"))
    result = asked.get("result") or next((e.get("event") for e in reversed(events) if e.get("event") == "done"), None)
    verified = _map(saved.get("verification")).get("verdict")
    decided = _map(asked.get("decided")).get("decision") or _map(live.get("decision")).get("decision")
    if found:
        investigate = ("done", f"{found.get('steps')} steps, {found.get('seconds')} s")
        diagnose = (
            ("done", f"confidence {diagnosis.get('confidence')}")
            if diagnosis
            else ("no", str(found.get("reason") or ""))
        )
    elif going:
        investigate, diagnose = ("now", f"step {live.get('steps') or 0}"), ("off", "not yet")
    else:
        investigate, diagnose = ("off", "not reached"), ("off", "not reached")
    out = [("done", alert.get("fired_at") or "") if alert else ("no", "no alert"), investigate, diagnose]
    if decided:
        out += [("done" if decided == "approved" else "no", decided)]
        out += [("done", "the action ran") if result == "done" else ("no", str(result or "nothing ran"))]
    elif live.get("left") is not None:
        out += [("now", "waiting for you"), ("off", "not yet")]
    elif request:  # asked, and nobody answered in time: no answer means denied
        out += [("no", "expired: no answer"), ("off", "nothing ran")]
    elif not gated:
        out += [("off", "not offered" if saved or not going else "not yet"), ("off", "nothing ran")]
    else:
        out += [("off", str(result or "nothing asked")), ("off", "nothing ran")]
    if verified:
        out.append(("done" if verified == "passed" else "no", verified))
    elif going and result == "done":
        out.append(("now", "health, then money"))
    else:
        out.append(("off", "not reached"))
    out.append(("done", "drafted") if has_postmortem else ("off", "none"))
    return [{"name": name, "state": state, "note": note} for name, (state, note) in zip(PHASES, out, strict=True)]


def run(folder: Path, root: Path = EVIDENCE, show: bool = False, now: float | None = None) -> dict:
    """One run, everything its page shows, as its folder is now. Files that are missing are left out; files that
    cannot be read are said. A run still going is shown from its trace and the gate's files (the live view)."""
    now = time.time() if now is None else now
    problems: list[str] = []
    saved = _object(folder, fault_runs.RUN_FILE, problems) or {}
    request = _object(folder, gate.REQUEST, problems)
    decision = _object(folder, gate.DECISION, problems)
    events = _records(folder, gate.LOG, problems)
    trace = _records(folder, loop.TRACE, problems)
    tools = _records(folder, loop.TOOL_RESULTS, problems)
    recording = _records(folder, loop.RECORDING, problems)
    replies = [r for r in recording if r.get("kind") in ("response", "failure")]
    versions = sorted(
        {str(_map(_map(r.get("details")).get("claude_code")).get("version")) for r in replies if r.get("details")}
        - {"None"}
    )
    stop = _map(next((r for r in trace if r.get("kind") == "stop"), None))
    start = _map(next((r for r in trace if r.get("kind") == "start"), None))
    alert = _map(saved.get("alert")) or _map(_map(start.get("extra")).get("alert")) or _map(_map(request).get("alert"))
    totals = _map(stop.get("totals"))
    found_loop = _map(saved.get("loop")) or (
        {"reason": stop.get("reason"), "detail": stop.get("detail"), "diagnosis": stop.get("diagnosis")}
        | {"steps": totals.get("steps"), "tool_calls": totals.get("tool_calls"), "seconds": totals.get("seconds")}
        if stop
        else {}
    )
    markdown_text = _text(folder, postmortem.MARKDOWN, problems)
    found = held(row(folder, root), show)
    if found.get("problem") and found["problem"] != NO_RUN_FILE:
        problems.append(found["problem"])
    going = in_progress(folder, now) and not folder.name.startswith(GATE_DEMO)
    left = waiting(request, decision, now)
    ended = _seconds(saved.get("ran_at_utc")) is not None and (folder / fault_runs.RUN_FILE).is_file()
    recent = ended and now - (folder / fault_runs.RUN_FILE).stat().st_mtime < LIVE_MINUTES * 60
    acted = _map(saved.get("gate")).get("result") == "done"
    # Approved, and the gate has not yet said how the action ended (found with make gate-demo, 2026-11-17).
    acting = _map(decision).get("decision") == "approved" and not any(e.get("event") in SETTLED for e in events)
    step_count = sum(1 for r in trace if r.get("kind") == "step")
    live_info = {"going": going, "steps": step_count, "request": request, "decision": decision, "events": events}
    live_info["left"] = left
    return {
        "folder": folder.name,
        "where": where(folder, root),
        "typed": evaluation.typed(folder),
        "problems": problems,
        "row": found,
        "saved": saved,
        "label": saved.get("label"),
        "recording_label": next((r.get("label") for r in recording if r.get("kind") == "header"), None),
        "trace_label": start.get("label"),
        "platform": saved.get("platform") or ("compose" if saved else None),
        "alert": alert,
        "signals": signals(saved),
        "loop": found_loop,
        "stop": stop,
        "steps": steps(trace, tools, replies),
        "diagnosis": _map(found_loop.get("diagnosis")) or _map(_map(request).get("diagnosis")),
        "versions": versions,
        "usage": evaluation.usage(folder),
        "budgets": _map(saved.get("budgets")) or _map(start.get("budgets")),
        "gate": _map(saved.get("gate")),
        "request": request,
        "decision": decision,
        "events": events,
        "left": left,
        "going": going,
        # Followed every 2 seconds while it goes on, while a request waits, and while a run that acted waits for its
        # postmortem (make postmortem, a little after the run): then the page stops asking.
        "live": going or left is not None or acting or (recent and acted and markdown_text is None),
        "version": version(folder),
        "simulation": folder.name.startswith(GATE_DEMO),
        "verification": _map(saved.get("verification")),
        "damage": damage(saved),
        "deploys": [d for d in saved.get("deploys") or [] if isinstance(d, Mapping)],
        "removed_by": saved.get("removed_by"),
        "postmortem": markdown_text,
        "reviewed": _text(folder, postmortem_review.REVIEWED, problems),
        "phases": phases(saved, alert, found_loop, markdown_text is not None, live_info),
    }


def report(names: Sequence[str], root: Path = EVIDENCE, show: bool = False) -> dict:
    """make eval-report's own report on these folders (klaxon.evaluation), and its page as it prints it: so each
    number on the evaluation page is the one make eval-report gives. Only the folders REPORT_FOLDERS names."""
    chosen = [name for name in REPORT_FOLDERS if name in names and (Path(root) / name).is_dir()]
    found = evaluation.report([Path(root) / name for name in chosen], show_judge=show)
    return {"folders": chosen, "found": found, "markdown": evaluation.markdown(found)}
