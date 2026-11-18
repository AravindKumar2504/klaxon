"""The panel's pages, made from the plain data klaxon.panel.runs reads. Every value from a run passes through esc().

Laid out like the concept mockup of 2026-10-19 (kept out of the repo): a run's alert and path, the signals at the
alert, the diagnosis with each piece of evidence linked to the step it cites, the steps, the approval request, the
verification and the postmortem. No script and no inline style: the page's Content-Security-Policy allows neither.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from klaxon import evaluation
from klaxon.panel import runs
from klaxon.panel.html import Html, el, href, join, markdown

LOGO = Html(
    '<svg width="26" height="26" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M6 17.5V13a6 6 0 0 1 12 0v4.5z" fill="#FF5A36"/>'
    '<path d="M9.2 12.6a2.8 2.8 0 0 1 2.8-2.8" stroke="#FFD1C4" stroke-width="1.4" fill="none" stroke-linecap="round"/>'
    '<rect x="4" y="17.5" width="16" height="3.2" rx="1.2" fill="#FF8A65"/>'
    '<path d="M12 2.2v2.2M4.6 5.1l1.5 1.5M19.4 5.1l-1.5 1.5M1.8 11.6h2.2M20 11.6h2.2" stroke="#FF8A65"'
    ' stroke-width="1.7" stroke-linecap="round"/></svg>'
)
NAV = (("Runs", "/runs"), ("Evaluation", "/evaluation"))
SHOWN = "shown"  # ?graders=shown: the graders' verdicts on runs Aravind has not labeled, as SHOW_JUDGE=1 shows them
FOOT = (
    "Klaxon's web panel reads the runs saved in evidence/ and shows them. It calls no tool, no stack and no model, and"
    " nothing on it changes anything: an approval is typed at a terminal (make approve). All data is synthetic: a"
    " simulated payment provider, no real money."
)


def _query(show: bool) -> dict:
    return {"graders": SHOWN} if show else {}


def chip(text: str, colour: str) -> Html:
    return el("span", el("span", class_=f"dot {colour}"), text, class_="chip")


def tag(text: str, kind: str = "real") -> Html:
    return el("span", text, class_=f"tag {kind}")


def card(title: str, *body: object, tags: Sequence[Html] = (), extra: str = "") -> Html:
    head = el("div", el("h3", title), el("div", list(tags), class_="r"), class_="hd")
    return el("section", head, *body, class_=f"card {extra}".strip())


def layout(title: str, on: str, *body: object, script: bool = False) -> Html:
    nav = el("nav", [el("a", name, href=link, class_="on" if name == on else None) for name, link in NAV], class_="nav")
    chips = el(
        "div",
        chip("127.0.0.1 only", "green"),
        chip("Shows only: nothing here changes anything", "blue"),
        chip("Synthetic data", "grey"),
        class_="tr",
    )
    top = el("header", el("a", LOGO, el("span", "Klaxon"), href="/runs", class_="brand"), nav, chips, class_="topbar")
    head = el(
        "head",
        el("meta", charset="utf-8"),
        el("meta", name="viewport", content="width=device-width, initial-scale=1"),
        el("meta", name="referrer", content="no-referrer"),
        el("title", title, " | Klaxon"),
        el("link", rel="stylesheet", href="/static/panel.css"),
        el("script", src="/static/live.js", defer=True) if script else None,
    )
    page = el("body", top, el("main", *body, class_="page"), el("footer", FOOT, class_="foot"))
    return join(Html("<!doctype html>\n"), el("html", head, page, lang="en"))


def _money(value: object) -> str:
    return "-" if not isinstance(value, int | float) or isinstance(value, bool) else f"${value:,.2f}"


def _plain(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, Mapping | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _verdict(value: object) -> Html:
    kind = {"names the cause": "y", "partly": "w", "misses the cause": "n"}.get(str(value), "p")
    return el("span", _plain(value), class_=kind)


def held_note(show: bool, link: str) -> Html:
    if show:
        return el(
            "p",
            "Graders' verdicts are shown on every run, at your request. ",
            el("a", "Hold them back again", href=link),
            ".",
            class_="note",
        )
    return el(
        "p",
        "On a run you have not labeled, neither grader's verdict shows, so your label stays blind (make label-pick saves"
        " no list of the runs it picks). ",
        el("a", "Show them", href=f"{link}?graders={SHOWN}"),
        ".",
        class_="note",
    )


# The runs list


def runs_page(rows: Sequence[Mapping], show: bool) -> Html:
    head = el(
        "tr",
        [
            el("th", name)
            for name in (
                "Run",
                "Kind",
                "Platform",
                "Model",
                "Case",
                "Seed",
                "Stopped",
                "Steps",
                "Calls",
                "Cost",
                "Rule",
                "Judge",
                "Yours",
            )
        ],
    )
    body = [_run_line(row, show) for row in rows]
    return layout(
        "Runs",
        "Runs",
        el("h1", "Runs"),
        el(
            "p",
            f"Every run saved in evidence/, newest first: {len(rows)}. Each is a recorded run, labeled as it was saved:"
            " a live run on this Mac with synthetic data, an evaluation run, or a rehearsal with no model.",
            class_="lead",
        ),
        held_note(show, "/runs"),
        el("div", el("table", el("thead", head), el("tbody", body), class_="list"), class_="scroll"),
    )


def _run_line(row: Mapping, show: bool) -> Html:
    link = el("a", row["folder"], href=href("run", row["folder"], **_query(show)), class_="mono")
    first = el("td", link, el("div", row.get("where", ""), class_="sub"))
    if row.get("problem"):
        return el("tr", first, el("td", row["problem"], colspan="12", class_="p"))
    if row.get("note"):  # a run still going, or make gate-demo's simulation (make panel ALSO=)
        return el("tr", first, el("td", row["note"], colspan="12", class_="w"))
    mode = evaluation.mode_cell(row) if row.get("mode") else "-"
    stopped = row.get("not_run") or row.get("loop") or ("rehearsal: no model" if row.get("rehearsal") else "-")
    graders = [el("td", "held back", class_="p"), el("td", "held back", class_="p")]
    if not row.get("held_back"):
        graders = [el("td", _verdict(row.get("verdict"))), el("td", _verdict(row.get("judge")))]
    cells = [
        mode,
        runs.PLATFORM_NAMES.get(str(row.get("platform")), row.get("platform")),
        row.get("model") or "-",
        row.get("case") or "-",
        _plain(row.get("seed")),
        stopped,
        _plain(row.get("steps")),
        _plain(row.get("calls")),
        _money(row.get("cost_usd")) if row.get("calls") else "-",
    ]
    return el("tr", first, [el("td", cell) for cell in cells], graders, el("td", _verdict(row.get("aravind"))))


# One run


def run_page(view: Mapping, show: bool) -> Html:
    link = href("run", view["folder"])
    problems = [el("li", problem) for problem in view["problems"]]
    if view["row"].get("problem"):
        problems.insert(0, el("li", view["row"]["problem"]))
    side = [_this_run(view), _approval(view), _verification(view)]
    main = [_signals(view), _diagnosis(view, show, link), _steps(view)]
    # The live view (week 7, item 2): /static/live.js reads this marker, asks the panel every 2 seconds whether the
    # run's folder changed (its state, below), and reloads the page when it did. It only reads.
    marker = el(
        "div",
        class_="live",
        data_state=href("run", view["folder"], "state"),
        data_version=view["version"],
        data_live="true" if view.get("live") else "false",
    )
    banner = None
    if view.get("live"):
        banner = el(
            "p",
            el("span", "LIVE", class_="sev on"),
            " This run is going on now, or waits for a person. The page follows its folder every 2 seconds.",
            class_="livebar",
        )
    return layout(
        view["folder"],
        "Runs",
        marker,
        banner,
        _header(view),
        card("Could not show everything", el("ul", problems, class_="said"), extra="warn") if problems else None,
        el("div", el("div", main, class_="col"), el("div", side, class_="col"), class_="grid"),
        _postmortem(view),
        script=True,
    )


def _header(view: Mapping) -> Html:
    alert = view["alert"] or {}
    pipe = []
    for number, phase in enumerate(view["phases"], 1):
        mark = "✓" if phase["state"] == "done" else ("✕" if phase["state"] == "no" else str(number))
        pipe.append(
            el(
                "li",
                el("i", mark),
                el("div", el("b", phase["name"]), el("span", phase["note"])),
                class_=f"st {phase['state']}",
            )
        )
    return el(
        "section",
        el(
            "div",
            el("span", "ALERT", class_="sev"),
            el("h1", alert.get("name") or "No alert"),
            tag("Simulation", "design") if view.get("simulation") else tag("Recorded run"),
            el("span", f"Fired {alert['fired_at']}" if alert.get("fired_at") else "", class_="when"),
            class_="row",
        ),
        el("p", alert.get("summary"), class_="summary") if alert.get("summary") else None,
        el("p", _label(view), class_="label") if _label(view) else None,
        el("ol", pipe, class_="pipe"),
        class_="card ihead",
    )


def _label(view: Mapping) -> str | None:
    """What the run's evidence is: its run.json's own label; a simulation's; or, for a run still going, what is
    known before its run.json comes."""
    if view.get("label"):
        return view["label"]
    if view.get("simulation"):
        return runs.SIMULATION
    if view.get("going"):
        return "A run going on now, on this Mac. Its run.json, with the run's full label, is written when it ends."
    return None


def _number(key: str, value: object) -> str:
    if isinstance(value, float) and "share" in key:
        return f"{value * 100:.1f}%"
    if isinstance(value, Mapping):
        return ", ".join(f"{k} {v}" for k, v in value.items()) or "none"
    return _plain(value)


def _signals(view: Mapping) -> Html | None:
    found = view.get("signals")
    if not found:
        return None
    tiles = []
    for check in found["checks"]:
        if not isinstance(check, Mapping):
            continue
        numbers = check.get("numbers") if isinstance(check.get("numbers"), Mapping) else {}
        tiles.append(
            el(
                "div",
                el("div", el("span", class_="dot red" if check.get("breached") else "dot green"), check.get("rule")),
                el("div", check.get("subject"), class_="tf"),
                [
                    el("div", el("span", key, class_="k"), " ", el("b", _number(key, value)))
                    for key, value in numbers.items()
                ],
                class_="tile bad" if check.get("breached") else "tile",
            )
        )
    said = (view.get("alert") or {}).get("summary")
    return card(
        "Signals at the alert",
        el("div", tiles, class_="tiles"),
        el("p", "The alert rules' own reading when the alert fired, at ", found.get("at_utc"), ".", class_="note"),
        el("p", said, class_="sigcap") if said else None,
        tags=[tag("Recorded")],
    )


def _graders(view: Mapping, show: bool, link: str) -> Html:
    row = view["row"]
    if row.get("problem") or row.get("note"):
        return el("p", "No grader graded it.", class_="note")
    if row.get("held_back"):
        return el(
            "p",
            "Graders' verdicts: held back until you label this run. ",
            el("a", "Show them", href=f"{link}?graders={SHOWN}"),
            ".",
            class_="note",
        )
    lines = [
        el("dt", "Rule (klaxon.grading)"),
        el("dd", _verdict(row.get("verdict")), "; fix fits: ", _plain(row.get("fix_fits"))),
        el("dt", "Judge (Claude, klaxon.judge)"),
        el("dd", _verdict(row.get("judge")), "; fix fits: ", _plain(row.get("judge_fix_fits"))),
        el("dt", "Yours (make label)"),
        el("dd", _verdict(row.get("aravind")), "; fix fits: ", _plain(row.get("aravind_fix_fits"))),
    ]
    return join(
        el("dl", lines, class_="kv"), el("p", row.get("judge_why"), class_="note") if row.get("judge_why") else None
    )


def _diagnosis(view: Mapping, show: bool, link: str) -> Html:
    found = view.get("diagnosis") or {}
    if not found:
        stop = view.get("stop") or {}
        if view.get("going") and not stop:
            return card("Diagnosis", el("p", "Not yet: the loop is still investigating."))
        return card(
            "Diagnosis",
            el("p", "No diagnosis. The loop stopped: ", stop.get("reason") or view["loop"].get("reason") or "-", "."),
            el("p", stop.get("meaning"), class_="note") if stop.get("meaning") else None,
        )
    confidence = found.get("confidence")
    fix = found.get("proposed_fix") if isinstance(found.get("proposed_fix"), Mapping) else {}
    action = found.get("fix_action") if isinstance(found.get("fix_action"), Mapping) else None
    evidence = []
    for item in found.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        cid = str(item.get("call_id") or "")
        evidence.append(
            el(
                "li",
                el("a", cid, href=f"#{cid}", class_="cid") if cid.startswith("c") and cid[1:].isdigit() else None,
                el("div", el("b", item.get("tool")), el("code", _plain(item.get("arguments"))), class_="tool"),
                el("div", item.get("shows"), class_="shows"),
                class_="ev",
            )
        )
    named = None
    if action:
        named = join(action.get("name"), " on ", (action.get("arguments") or {}).get("service", "-"))
    return card(
        "Diagnosis",
        el(
            "p",
            el("span", f"confidence {_plain(confidence)}", class_="conf"),
            el("meter", min="0", max="1", value=_plain(confidence)) if isinstance(confidence, int | float) else None,
            class_="row",
        ),
        _graders(view, show, link),
        el("div", el("div", found.get("hypothesis")), class_="quote"),
        el(
            "dl",
            el("dt", "Proposed fix"),
            el("dd", fix.get("kind"), " on ", fix.get("target"), ": ", fix.get("summary")),
            el("dt", "Action named"),
            el("dd", named or "none"),
            el("dt", "Blast radius"),
            el("dd", found.get("blast_radius")),
            el("dt", "Rollback plan"),
            el("dd", found.get("rollback_plan")),
            class_="kv wide",
        ),
        el("p", "Proposed, not applied: the loop itself never applies a fix.", class_="note"),
        el("div", "Evidence, each cited by its tool call", class_="lbl"),
        el("ul", evidence, class_="evidence"),
        tags=[tag("Made up for the demo", "design") if view.get("simulation") else tag("Recorded Claude response")],
    )


def _bar(steps: Sequence[Mapping]) -> Html | None:
    widths = [s.get("step_seconds") if isinstance(s.get("step_seconds"), int | float) else 0 for s in steps]
    total = sum(widths)
    if not steps or total <= 0:
        return None
    x, rects = 0.0, []
    for step, width in zip(steps, widths, strict=True):
        share = 1000 * width / total
        kind = "tool" if step.get("tool") else ("dx" if step.get("allowed") is True else "bad")
        rects.append(
            el(
                "g",
                el("title", f"step {step.get('step')}: {_plain(step.get('step_seconds'))} s"),
                el("rect", x=f"{x:.1f}", y="0", width=f"{max(share - 4, 2):.1f}", height="26", rx="5", class_=kind),
                el("text", _plain(step.get("step")), x=f"{x + share / 2:.1f}", y="17", class_="segt"),
            )
        )
        x += share
    return el("svg", rects, viewBox="0 0 1000 26", preserveAspectRatio="none", class_="tbar", role="img")


def _steps_of(used: object, steps: object, role_steps: object) -> str | None:
    """A multi-agent run's steps (week 8): its investigator's budget and the other roles', together; None for the
    single loops, whose budget is the run's steps alone."""
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (steps, role_steps)):
        return None
    return f"{_plain(used)} of {steps + role_steps} ({steps} for the investigator, {role_steps} for the other roles)"


def _steps(view: Mapping) -> Html:
    stop = view.get("stop") or {}
    totals = stop.get("totals") if isinstance(stop.get("totals"), Mapping) else {}
    items = []
    for step in view["steps"]:
        anchor = step.get("call_id") if step.get("call_id") else f"step-{step.get('step')}"
        what = step.get("tool") or step.get("action") or "a reply Klaxon could not use"
        ok = step.get("allowed") is True
        items.append(
            el(
                "details",
                el(
                    "summary",
                    el("b", f"{step.get('step')}. "),
                    f"{step['role']}: " if step.get("role") else None,
                    el("span", step.get("call_id") or "", class_="cid") if step.get("call_id") else None,
                    " " if step.get("call_id") or not step.get("role") else None,
                    what,
                    el("span", f" {_plain(step.get('step_seconds'))} s", class_="p"),
                    el("span", "" if ok else " sent back", class_="n"),
                ),
                el(
                    "dl",
                    el("dt", "Klaxon's reason (the model's words)"),
                    el("dd", step.get("why") or "-"),
                    el("dt", "Code's decision"),
                    el("dd", "allowed: " if ok else "refused: ", step.get("decision") or "-"),
                    el("dt", "Tool and arguments") if step.get("tool") else None,
                    el("dd", el("code", step.get("tool"), " ", _plain(step.get("arguments"))))
                    if step.get("tool")
                    else None,
                    class_="kv wide",
                ),
                el("div", "What came back (untrusted data)", class_="lbl") if step.get("result") is not None else None,
                el("pre", step.get("result")) if step.get("result") is not None else None,
                el("div", "The reply, as received", class_="lbl") if step.get("reply") else None,
                el("pre", step.get("reply")) if step.get("reply") else None,
                id=anchor,
                class_="step",
            )
        )
    said = f"Going on now: {len(view['steps'])} steps so far." if view.get("going") else "-"
    if stop:
        said = (
            f"Stopped: {stop.get('reason')} ({stop.get('detail') or stop.get('meaning') or ''}). "
            f"{_plain(totals.get('steps'))} steps, {_plain(totals.get('seconds'))} s"
        )
    return card(
        "Steps",
        el("p", said, class_="note"),
        _bar(view["steps"]),
        el("div", items, class_="steps") if items else el("p", "No step: the loop did not run.", class_="note"),
        el("p", view.get("trace_label"), class_="note") if view.get("trace_label") else None,
        tags=[tag("Recorded")],
    )


def _this_run(view: Mapping) -> Html:
    row, budgets, used = view["row"], view.get("budgets") or {}, view.get("usage") or {}
    found = view.get("loop") or {}

    def of(value: object, key: str, unit: str = "") -> str:
        limit = budgets.get(key)
        return f"{_plain(value)}{unit}" + (f" of {_plain(limit)}{unit}" if limit is not None else "")

    lines = [
        ("Kind", evaluation.mode_cell(row) if row.get("mode") else ("going on now" if view.get("going") else "-")),
        ("Platform", runs.PLATFORM_NAMES.get(str(view.get("platform")), view.get("platform") or "-")),
        ("Model", row.get("model") or ", ".join(used.get("models") or []) or "-"),
        ("Through", ", ".join(f"Claude Code {v}" for v in view.get("versions") or []) or "-"),
        (
            "Steps",
            _steps_of(found.get("steps"), budgets.get("steps"), view.get("role_steps"))
            or of(found.get("steps"), "steps"),
        ),
        ("Tool calls", of(found.get("tool_calls"), "tool_calls")),
        ("Time", of(found.get("seconds"), "seconds", " s")),
        ("Model calls", f"{_plain(used.get('calls'))} ({_plain(used.get('failed'))} failed)"),
        ("Cost estimate", f"{_money(used.get('cost_usd'))} at API prices *" if used.get("calls") else "-"),
        ("Folder", view["where"]),
    ]
    if view.get("simulation"):
        return card(
            "This run",
            el("p", runs.SIMULATION),
            el("p", "No trace and no model call: nothing to replay.", class_="note"),
        )
    traced = bool(view.get("trace_label") or view.get("stop") or view.get("steps"))
    return card(
        "This run",
        el("dl", [join(el("dt", key), el("dd", value)) for key, value in lines], class_="kv"),
        el("div", "Replay it offline", class_="lbl") if traced and not view.get("going") else None,
        el("pre", f"make replay-run RUN={view['typed']}", class_="term") if traced and not view.get("going") else None,
        el("p", "* Claude Code's own estimate. The seat is not billed per call.", class_="note"),
        el("p", view.get("recording_label"), class_="note") if view.get("recording_label") else None,
    )


def _approval(view: Mapping) -> Html:
    asked, request, decision = view.get("gate") or {}, view.get("request") or {}, view.get("decision") or {}
    saved = view.get("saved") or {}
    if not request and view.get("going"):
        return card(
            "Approval",
            el("p", "No request yet."),
            el("p", "Klaxon can ask. Only a person approves, typed at a terminal.", class_="note"),
        )
    if not request and not saved.get("gated"):
        return card(
            "Approval",
            el("p", "No action was offered: this run could only diagnose."),
            el("p", "Klaxon can ask. Only a person approves, typed at a terminal.", class_="note"),
        )
    if not request:
        return card(
            "Approval",
            el("p", "No request was made: ", asked.get("result") or "-", ". ", asked.get("detail") or ""),
            el("p", "Klaxon can ask. Only a person approves, typed at a terminal.", class_="note"),
        )
    action = request.get("action") if isinstance(request.get("action"), Mapping) else {}
    requested = next((e for e in view["events"] if e.get("event") == "requested"), {})
    trail = [
        el(
            "li",
            el("span", event.get("at"), class_="mono"),
            " ",
            el("b", event.get("event")),
            " ",
            event.get("how") or "",
        )
        for event in view["events"]
    ]
    command = f"make approve RUN={view['typed']}"
    pending = view.get("left") is not None
    said = decision.get("decision") or ("none yet" if pending else "none: no answer in time means denied")
    waits = None
    if pending:
        left = int(view["left"])
        waits = el(
            "p",
            el("b", "Waiting for you. "),
            "Approve or deny it in your own terminal, with the command below. ",
            el(
                "span",
                f"expires in {left // 60}:{left % 60:02d}",
                class_="timer",
                data_expires=request.get("expires_at"),
            ),
            class_="waits",
        )
    return card(
        "Approval request",
        waits,
        el("div", "Klaxon asked to run", class_="lbl"),
        el(
            "p",
            el("span", action.get("name"), class_="mono big"),
            " on ",
            el("span", (action.get("arguments") or {}).get("service"), class_="svc"),
            class_="act",
        ),
        el("div", "The change", class_="lbl"),
        el("p", requested.get("change") or asked.get("detail") or "-", class_="mono small"),
        el("div", "Undo", class_="lbl"),
        el("p", requested.get("undo") or "-", class_="small-q"),
        el(
            "dl",
            el("dt", "Request"),
            el("dd", request.get("id")),
            el("dt", "Made"),
            el("dd", request.get("created_at")),
            el("dt", "Expires"),
            el("dd", request.get("expires_at")),
            el("dt", "Decision"),
            el("dd", el("b", said), " ", decision.get("how") or "", " ", decision.get("at") or ""),
            el("dt", "Outcome"),
            el("dd", asked.get("result") or "-"),
            class_="kv",
        ),
        el("div", "The gate's trail (gate.jsonl)", class_="lbl"),
        el("ul", trail, class_="trail"),
        el("div", "Approved or denied in a terminal, never here", class_="lbl"),
        el("pre", command, class_="term"),
        el("button", "Copy command", type="button", class_="btn", data_copy=command) if pending else None,
        el("p", "Klaxon can ask. Only a person approves. No answer in 10 minutes means denied.", class_="note"),
        tags=[tag("Simulation", "design") if view.get("simulation") else tag("Recorded")],
        extra="appr",
    )


def _verification(view: Mapping) -> Html | None:
    found, losses = view.get("verification") or {}, view.get("damage") or []
    if not found and not losses:
        return None
    invariants = sorted({name for reading in losses for name in reading["findings"]})
    table = None
    if losses:
        head = el("tr", el("th", "Money check"), el("th", "Outcome"), [el("th", name) for name in invariants])
        body = [
            el(
                "tr",
                el("td", reading["when"]),
                el("td", reading["outcome"]),
                [el("td", reading["findings"].get(name, 0)) for name in invariants],
            )
            for reading in losses
        ]
        table = el("div", el("table", el("thead", head), el("tbody", body), class_="vt"), class_="scroll")
    deploys = [
        el(
            "li",
            el("span", deploy.get("at"), class_="mono"),
            " ",
            deploy.get("service"),
            " by ",
            deploy.get("by"),
            ": ",
            ", ".join(f"{k}={v}" for k, v in (deploy.get("settings") or {}).items()),
        )
        for deploy in view.get("deploys") or []
    ]
    verdict = found.get("verdict")
    return card(
        "Verification",
        el(
            "p",
            el("b", "Verdict: "),
            el("span", verdict or "not run", class_={"passed": "y", "failed": "n"}.get(verdict, "p")),
        )
        if found
        else None,
        el("ul", [el("li", line) for line in found.get("said") or []], class_="said") if found.get("said") else None,
        table,
        el("div", "Deploys during the run (the deploy log)", class_="lbl") if deploys else None,
        el("ul", deploys, class_="trail") if deploys else None,
        el("p", view.get("removed_by"), class_="note") if view.get("removed_by") else None,
        tags=[tag("Recorded")],
    )


def _postmortem(view: Mapping) -> Html | None:
    if not view.get("postmortem"):
        return None
    if view["row"].get("held_back"):  # the draft states the rule's verdict: held back with the graders' (finding 3)
        return card(
            "Postmortem draft",
            el(
                "p",
                "Held back until you label this run: the draft states the rule's verdict. ",
                el("a", "Show it", href=f"{href('run', view['folder'])}?graders={SHOWN}"),
                ".",
                class_="note",
            ),
            extra="wide",
        )
    marks = None
    if view.get("reviewed"):
        marks = el(
            "details", el("summary", "Your marks on this draft (make review-postmortem)"), markdown(view["reviewed"])
        )
    return card(
        "Postmortem draft",
        el("div", markdown(view["postmortem"]), class_="md"),
        marks,
        tags=[tag("Recorded Claude response")],
        extra="wide",
    )


# The evaluation report


def evaluation_page(found: Mapping, show: bool) -> Html:
    boxes = [
        el(
            "label",
            el("input", type="checkbox", name="from", value=name, checked=name in found["folders"]),
            f" evidence/{name}",
        )
        for name in runs.REPORT_FOLDERS
    ]
    form = el(
        "form",
        boxes,
        el(
            "label",
            el("input", type="checkbox", name="graders", value=SHOWN, checked=show),
            " show the judge's verdicts",
        ),
        el("button", "Show", type="submit"),
        method="get",
        action="/evaluation",
        class_="pick",
    )
    held = None
    if not show:
        held = el(
            "p",
            "As make eval-report does, the judge's verdict on a run you have not labeled is held back; the rule's are"
            " shown, as each suite's own report shows them.",
            class_="note",
        )
    return layout(
        "Evaluation",
        "Evaluation",
        el("h1", "Evaluation"),
        el(
            "p",
            "make eval-report's own report (klaxon.evaluation), on the folders chosen below: the same code, so the same"
            " numbers.",
            class_="lead",
        ),
        form,
        held,
        el("div", markdown(found["markdown"]), class_="card md"),
    )


def not_found(name: str) -> Html:
    return layout(
        "Not found",
        "Runs",
        el("h1", "No such run"),
        el("p", "No saved run in evidence/ has the name ", el("code", name), "."),
        el("p", el("a", "All runs", href="/runs")),
    )
