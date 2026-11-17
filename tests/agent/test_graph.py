"""Klaxon's LangGraph loop (week 6, item 2), offline: the same loop as the plain one, with its control flow in a graph.

The loop's behavior tests (tests/agent/test_loop.py) run on both loops. Here: every saved live run of weeks 3 to 6,
replayed on the LangGraph loop exactly as the plain loop replays it (the recorded replies, received from Claude, and the
recorded tool results, served again: nothing is called); the graph's shape and its drawing; its guards (the recursion
limit from the budgets, LangSmith's tracing refused, the caller's thread, no network); how a run says which loop ran
it, and how a replay picks its loop; and that the plain loop needs no LangGraph. The model replies made up here are
scripted (ScriptedModel); none came from a model.
"""

import os
import subprocess
import sys
import threading
import tomllib
from collections import Counter
from importlib.metadata import version

import pytest
from langgraph.errors import GraphRecursionError
from langsmith.run_helpers import tracing_context

from klaxon import evaluation, fault_runs
from klaxon.agent import catalog, graph, loop
from klaxon.agent import mcp_client as mc
from klaxon.agent.model import Recording, ScriptedModel
from klaxon.agent.trace import read_trace
from tests.agent.test_loop import ALERT, MADE_UP, MadeUpLive, RawTools, Served, diagnose, tool

REPO = loop.REPO
EVIDENCE = REPO / "evidence"
SAVED = sorted(trace.parent for trace in EVIDENCE.glob("week*/**/trace.jsonl"))
# The one saved run the plain loop cannot replay: week 3's first live run, whose system prompt changed after it (week 3,
# item 4), so no recorded reply matches it; the plain loop's own test skips it. Each loop must fail it the same way.
NOT_REPLAYED = {"week3/2026-10-18T112951Z_loop_ledgerlaghigh"}
# Week 5's one run that ended in a rollback with no action named (Sonnet 5.5, pool exhaustion, seed 8).
UNNAMED_ROLLBACK = (
    EVIDENCE / "week5/2026-11-07T070612Z_suite_claude-sonnet-5-5/2026-11-07T072837Z_pool-exhaustion_seed8"
)


def shown(folder):
    return str(folder.relative_to(EVIDENCE))


# Every saved live run, replayed on the LangGraph loop


def test_every_saved_live_run_of_weeks_3_to_5_is_replayed_here():
    # So the replays below can never shrink unseen. Week 6's runs join as they are saved, each replayed the same way.
    weeks = Counter(folder.relative_to(EVIDENCE).parts[0] for folder in SAVED)
    assert (weeks["week3"], weeks["week4"], weeks["week5"]) == (4, 9, 48)
    assert NOT_REPLAYED <= {shown(folder) for folder in SAVED}


# What may differ between two replays of one run: the clock's readings, the trace's own id, and the loop's own name.
TIMES = ("at", "t", "run_id", "step_seconds", "tool_seconds")


def comparable(records):
    out = []
    for record in records:
        kept = {key: value for key, value in record.items() if key not in TIMES}
        if record["kind"] == "start":
            about = {k: v for k, v in kept["extra"]["loop"].items() if k not in ("kind", "langgraph_version")}
            kept["extra"] = kept["extra"] | {"loop": about}
        if record["kind"] == "stop":
            kept["totals"] = {k: v for k, v in kept["totals"].items() if k not in ("seconds", "tool_seconds")}
        out.append(kept)
    return out


@pytest.mark.parametrize("folder", SAVED, ids=shown)
def test_the_langgraph_loop_replays_each_saved_live_run_exactly_as_the_plain_loop_does(folder, tmp_path):
    """Replies received from Claude through Claude Code on Aravind's seat (weeks 3 to 6), and each run's tool results,
    served again to both loops. Nothing is called. Where the plain loop reproduces a run, the LangGraph loop must too,
    and both replays' traces must be the same, record by record, but for the clock, the ids and the loop's name."""
    plain = loop.replay(folder, tmp_path / "plain.jsonl", loop.PLAIN)
    langgraph = loop.replay(folder, tmp_path / "graph.jsonl", loop.LANGGRAPH)
    (result, whole, differences), (again, whole_again, differences_again) = plain, langgraph
    assert (whole_again, differences_again) == (whole, differences)
    kept = ("reason", "steps", "tool_calls", "diagnosis", "sent_back")
    assert {key: getattr(again, key) for key in kept} == {key: getattr(result, key) for key in kept}
    replayed = read_trace(tmp_path / "graph.jsonl")
    assert replayed[0]["extra"]["loop"]["kind"] == loop.LANGGRAPH
    assert comparable(replayed) == comparable(read_trace(tmp_path / "plain.jsonl"))
    if shown(folder) in NOT_REPLAYED:
        assert differences[0] == "the system prompt differs"  # as the plain loop's own test finds, and skips
    else:
        assert (whole, differences) == (True, [])  # exactly: the same prompts, replies, decisions, results and stop


def test_week_5s_rollback_with_no_action_named_replays_on_the_langgraph_loop_without_the_check(tmp_path):
    # Recorded before the check existed: a replay applies the checks a run had, none, on either loop.
    result, whole, differences = loop.replay(UNNAMED_ROLLBACK, tmp_path / "t.jsonl", loop.LANGGRAPH)
    assert (whole, differences, result.sent_back) == (True, [], ())
    assert result.diagnosis.proposed_fix.kind == "rollback" and result.diagnosis.fix_action is None


# The graph: the plain loop's parts as nodes, its stop rules as edges


def test_the_graph_has_the_loops_parts_as_nodes_and_an_edge_to_the_end_for_each_stop_rule():
    drawn = graph.build(dict.fromkeys(graph.NODES, graph._never)).get_graph()
    assert set(drawn.nodes) == {"__start__", "__end__", *graph.NODES}
    assert graph.NODES == ("open_tools", "budgets", "ask_model", "read_reply", "rollback_check", "call_tool")
    edges = {(edge.source, edge.target, edge.data) for edge in drawn.edges}
    assert ("__start__", "open_tools", None) in edges
    assert {(source, label) for source, target, label in edges if target == "__end__"} == set(graph.STOPPED.items())
    assert edges - {(s, t, d) for s, t, d in edges if t == "__end__"} == {
        ("__start__", "open_tools", None),
        ("open_tools", "budgets", "first step"),
        ("budgets", "ask_model", "a step left"),
        ("ask_model", "budgets", "no reply: again"),
        ("ask_model", "read_reply", "a reply"),
        ("read_reply", "budgets", "invalid: again"),
        ("read_reply", "rollback_check", "a diagnosis"),
        ("read_reply", "call_tool", "a tool call"),
        ("rollback_check", "budgets", "sent back"),
        ("call_tool", "budgets", "a result"),
    }


def test_the_graph_reuses_the_plain_loops_code_for_every_part_and_moves_only_the_control_flow():
    # Each part is the plain loop's own method, never a copy: the graph's class adds its kind, run() and the nodes.
    own = {name for name in vars(graph.GraphInvestigation) if not name.startswith("__")}
    nodes = {"_open_tools", "_budgets", "_ask_model", "_read_reply", "_rollback_check", "_call_tool"}
    assert own == {"kind", "about", "run", "graph", *nodes}
    assert issubclass(graph.GraphInvestigation, loop.Investigation)


def test_the_drawing_is_langgraphs_own_drawing_of_the_graph_a_run_uses(capsys):
    run = graph.GraphInvestigation(ALERT, ScriptedModel([]), RawTools(), None, loop.Budgets(), lambda: 0.0, {})
    assert graph.draw() == run.graph().get_graph().draw_mermaid()
    assert graph.main(["draw"]) == 0
    out = capsys.readouterr().out
    assert out == graph.draw() and "graph TD;" in out
    for node, label in graph.STOPPED.items():  # each stop rule, an edge to the end, labeled
        assert f"\t{node} -. &nbsp;{label}&nbsp; .-> __end__;\n" in out
    assert graph.main([]) == 2


def test_make_loop_graph_prints_the_drawing_and_calls_nothing():
    shown_ = subprocess.run(["make", "-n", "loop-graph"], cwd=REPO, capture_output=True, text=True, check=True)
    assert shown_.stdout.strip() == ".venv/bin/python -m klaxon.agent.graph draw"


# Its guards


def test_the_longest_run_the_budgets_allow_ends_on_its_own_rule_never_on_langgraphs_limit(tmp_path):
    # 50 steps, each a tool call: the tools opened, then 4 nodes a step, the last step's tool call refused. 201 nodes.
    budgets = loop.Budgets(steps=50, tool_calls=50)
    result = loop.investigate(
        ALERT, ScriptedModel([tool()] * 50), Served().client, tmp_path / "t.jsonl", budgets, loop_kind=loop.LANGGRAPH
    )
    assert (result.reason, result.steps, result.tool_calls) == ("step_budget", 50, 49)
    assert graph.recursion_limit(budgets) == 202  # LangGraph's limit counts the input too: 201 nodes, and one


def test_a_graph_that_would_run_more_nodes_than_the_budgets_allow_stops_as_a_crash(tmp_path, monkeypatch):
    # A stand-in for a bug that loops without using a step: LangGraph's limit, set from the budgets, ends the run.
    monkeypatch.setattr(graph, "NODE_RUNS_PER_STEP", 2)
    with pytest.raises(GraphRecursionError):
        loop.investigate(
            ALERT,
            ScriptedModel([tool()] * 5),
            Served().client,
            tmp_path / "t.jsonl",
            loop.Budgets(steps=5),
            loop_kind=loop.LANGGRAPH,
        )
    stop = read_trace(tmp_path / "t.jsonl")[-1]
    assert (stop["reason"], stop["detail"].split(":")[0]) == ("crashed", "GraphRecursionError")


# LangSmith's switches, as langsmith.utils.tracing_is_enabled reads them (0.14.12): written out here, not read from the
# code under test, so a switch left out of graph.TRACING is caught.
SWITCHES = ["LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING"]


@pytest.mark.parametrize("name", SWITCHES)
@pytest.mark.parametrize("value", ["true", "TRUE", " true "])
def test_the_langgraph_loop_refuses_to_run_while_the_environment_switches_langsmith_tracing_on(
    tmp_path, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    model, tools = ScriptedModel([tool()]), RawTools()
    with pytest.raises(loop.LoopUnavailable, match=f"LangSmith tracing is switched on \\({name}\\)"):
        loop.investigate(ALERT, model, tools, tmp_path / "t.jsonl", loop_kind=loop.LANGGRAPH)
    assert (model.calls, tools.opened_with, (tmp_path / "t.jsonl").exists()) == ([], None, False)


def test_the_langgraph_loop_refuses_to_run_while_langsmiths_own_switch_turns_tracing_on(tmp_path):
    model, tools = ScriptedModel([tool()]), RawTools()
    with tracing_context(enabled=True), pytest.raises(loop.LoopUnavailable, match="LangSmith's own switch"):
        loop.investigate(ALERT, model, tools, tmp_path / "t.jsonl", loop_kind=loop.LANGGRAPH)
    assert (model.calls, tools.opened_with) == ([], None)


@pytest.mark.parametrize("value", ["false", "", "0"])
def test_tracing_switched_off_lets_the_langgraph_loop_run(tmp_path, monkeypatch, value):
    for name in SWITCHES:
        monkeypatch.setenv(name, value)
    result = loop.investigate(
        ALERT, ScriptedModel([tool(), diagnose("c1")]), Served().client, tmp_path / "t.jsonl", loop_kind=loop.LANGGRAPH
    )
    assert result.reason == "diagnosed"


def test_without_langgraph_installed_the_langgraph_loop_is_refused_saying_how_to_install_it(tmp_path, monkeypatch):
    # As in a venv without the extra: importing klaxon.agent.graph afresh fails at its first `from langgraph ...`.
    for name in ("langgraph", "langgraph.graph"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "klaxon.agent.graph")
    monkeypatch.delattr(sys.modules["klaxon.agent"], "graph")
    model, tools = ScriptedModel([tool()]), RawTools()
    with pytest.raises(loop.LoopUnavailable, match="needs LangGraph installed") as raised:
        loop.investigate(ALERT, model, tools, tmp_path / "t.jsonl", loop_kind=loop.LANGGRAPH)
    assert loop.INSTALL_GRAPH in str(raised.value) and "requirements/graph.lock.txt" in loop.INSTALL_GRAPH
    assert (model.calls, tools.opened_with, (tmp_path / "t.jsonl").exists()) == ([], None, False)
    assert loop.runner(loop.PLAIN) is loop.Investigation  # the plain loop runs all the same


@pytest.mark.parametrize("kind", ["LangGraph", "graph", "", None, 7])
def test_a_loop_this_code_does_not_know_is_refused_before_anything(tmp_path, kind):
    model, tools = ScriptedModel([tool()]), RawTools()
    with pytest.raises(ValueError, match="no loop"):
        loop.investigate(ALERT, model, tools, tmp_path / "t.jsonl", loop_kind=kind)
    assert (model.calls, tools.opened_with, (tmp_path / "t.jsonl").exists()) == ([], None, False)


def test_every_node_runs_in_the_callers_thread_so_an_interrupt_stops_the_step_it_is_in(tmp_path):
    threads = []

    def where(system, turns):
        threads.append(threading.get_ident())
        return tool() if len(threads) == 1 else diagnose("c1")

    served = Served(
        {"service_health": lambda args: (threads.append(threading.get_ident()), MADE_UP["service_health"])[1]}
    )
    result = loop.investigate(
        ALERT, ScriptedModel([where, where]), served.client, tmp_path / "t.jsonl", loop_kind=loop.LANGGRAPH
    )
    assert result.reason == "diagnosed" and len(threads) == 3 and set(threads) == {threading.get_ident()}


NO_NETWORK = """
import socket, sys
def refuse(*args, **kwargs):
    raise SystemExit("a network connection was opened")
socket.socket.connect = socket.socket.connect_ex = refuse
socket.create_connection = socket.getaddrinfo = refuse
import tempfile
from pathlib import Path
from klaxon.agent import loop
with tempfile.TemporaryDirectory() as scratch:
    result, whole, differences = loop.replay(sys.argv[1], Path(scratch) / "t.jsonl", loop.LANGGRAPH)
print(result.reason, whole, differences, "langgraph" in sys.modules)
"""


def test_a_replay_on_the_langgraph_loop_imports_and_runs_it_with_no_network_connection():
    # In a process of its own, with sockets refused from before LangGraph is imported: nothing of a run leaves this Mac.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGSMITH_", "LANGCHAIN_"))}
    done = subprocess.run(
        [sys.executable, "-c", NO_NETWORK, str(UNNAMED_ROLLBACK)], capture_output=True, text=True, env=env, timeout=120
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "diagnosed True [] True"


def test_the_plain_loop_and_the_harness_never_import_langgraph():
    """Week 6, his choice: LangGraph is an optional extra; the plain loop, the fault runs and the evaluation need none
    of it until a run asks for the LangGraph loop."""
    code = (
        "import sys, klaxon.agent.loop, klaxon.fault_runs, klaxon.evaluation;"
        " from klaxon.agent import loop;"
        " print(sorted(n for n in sys.modules if n.split('.')[0] in ('langgraph', 'langchain_core', 'langsmith')),"
        " loop.runner(loop.PLAIN).__name__)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert done.stdout.strip() == "[] Investigation"


# Which loop ran a run, and which one replays it


def record(folder, script, loop_kind, actions=()):
    folder.mkdir(parents=True)
    model = MadeUpLive(Recording(folder / loop.RECORDING), script)
    tools = mc.RecordedTools(Served().client, folder / loop.TOOL_RESULTS)
    return loop.investigate(ALERT, model, tools, folder / loop.TRACE, actions=actions, loop_kind=loop_kind)


@pytest.mark.parametrize("made_on", loop.LOOPS)
def test_a_replay_uses_the_loop_its_run_recorded_unless_told_and_either_loop_replays_the_other(tmp_path, made_on):
    record(tmp_path / "run", [tool(), diagnose("c1")], made_on)
    for asked, used in [(None, made_on), *((kind, kind) for kind in loop.LOOPS)]:
        out = tmp_path / f"replay-{asked}.jsonl"
        result, whole, differences = loop.replay(tmp_path / "run", out, asked)
        assert (result.reason, whole, differences) == ("diagnosed", True, [])
        assert read_trace(out)[0]["extra"]["loop"]["kind"] == used


def test_a_run_recorded_before_the_trace_named_its_loop_replays_on_the_plain_loop():
    assert loop.recorded_loop({}) == loop.PLAIN
    assert loop.recorded_loop({"kind": "langgraph"}) == loop.LANGGRAPH
    for kind in ("graph", None, ["plain"], 7):
        with pytest.raises(ValueError, match="does not know"):
            loop.recorded_loop({"kind": kind})


def test_make_replay_run_says_which_loop_replayed_the_run(tmp_path, capsys):
    record(tmp_path / "run", [tool(), diagnose("c1")], loop.LANGGRAPH)
    assert loop.main(["replay", str(tmp_path / "run")]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Replayed by the LangGraph loop (LangGraph {graph.VERSION}).\n") and "Reproduced:" in out
    assert loop.main(["replay", str(tmp_path / "run"), "--loop", "plain"]) == 0
    assert capsys.readouterr().out.startswith("Replayed by the plain loop.\n")


def test_a_replay_on_a_loop_that_cannot_run_here_is_refused_saying_why(tmp_path, monkeypatch, capsys):
    record(tmp_path / "run", [tool(), diagnose("c1")], loop.PLAIN)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert loop.main(["replay", str(tmp_path / "run"), "--loop", "langgraph"]) == 2
    assert "Cannot replay" in capsys.readouterr().err
    assert loop.main(["replay", str(tmp_path / "run")]) == 0  # the plain loop needs no LangGraph, nor its switches


def test_a_live_runs_loop_reaches_investigate_and_its_trace(tmp_path, monkeypatch, live_calls_allowed):
    seen = {}
    real = loop.investigate

    def investigate(*args, **kwargs):
        seen["loop_kind"] = kwargs.get("loop_kind")
        return real(
            args[0], ScriptedModel([tool(), diagnose("c1")]), Served().client, args[3], loop_kind=seen["loop_kind"]
        )

    monkeypatch.setattr(loop, "investigate", investigate)
    found = loop.investigate_live(
        ALERT, loop.Budgets(), tmp_path, {}, "claude-opus-5-5", tmp_path / "claude", loop_kind=loop.LANGGRAPH
    )
    assert seen == {"loop_kind": loop.LANGGRAPH} and found.reason == "diagnosed"
    assert read_trace(tmp_path / loop.TRACE)[0]["extra"]["loop"]["kind"] == loop.LANGGRAPH


def test_the_run_header_names_the_loop_and_for_the_langgraph_loop_its_version():
    assert loop.loop_header(loop.PLAIN) == {"loop_kind": "plain"}
    assert loop.loop_header(loop.LANGGRAPH) == {"loop_kind": "langgraph", "langgraph_version": graph.VERSION}
    with pytest.raises(ValueError, match="no loop"):
        loop.loop_header("graph")


def test_a_run_offered_the_rollback_on_the_langgraph_loop_sends_a_rollback_with_no_action_back_once(tmp_path):
    # The rollback check, a node of its own: the plain loop's code, so the same words and the same once.
    found = record(
        tmp_path / "run", [tool(), diagnose("c1"), diagnose("c1")], loop.LANGGRAPH, [catalog.ROLLBACK_DEPLOY]
    )
    assert (found.reason, found.steps, found.sent_back) == ("diagnosed", 3, (loop.ROLLBACK_NAMED,))
    steps = [r for r in read_trace(tmp_path / "run" / loop.TRACE) if r["kind"] == "step"]
    assert [s["why"] for s in steps][1:] == [loop.SENT_BACK, loop.TAKEN + loop.STILL_UNNAMED]


# LangGraph, pinned: an optional extra, with every library it brings locked


def test_langgraph_is_an_optional_extra_pinned_exactly_with_every_library_it_brings_locked():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == [] and project["optional-dependencies"]["graph"] == ["langgraph==1.2.18"]
    lock = [
        line.split("#")[0].strip()
        for line in (REPO / "requirements" / "graph.lock.txt").read_text().splitlines()
        if line.split("#")[0].strip()
    ]
    assert "langgraph==1.2.18" in lock and all(line.count("==") == 1 for line in lock) and len(lock) == 38
    assert graph.VERSION == "1.2.18"  # the venv runs what the lock says
    pinned = dict(line.split("==") for line in lock)
    assert all(version(name) == wanted for name, wanted in pinned.items())


def test_make_venv_installs_the_graph_extra_with_its_lock():
    shown_ = subprocess.run(["make", "-n", "venv"], cwd=REPO, capture_output=True, text=True, check=True)
    assert ".venv/bin/pip install -c requirements/graph.lock.txt -e '.[dev,services,graph]'" in shown_.stdout


MADE = [("investigate-fault", ["FAULT=poison-message"]), ("eval", []), ("replay-run", ["RUN=x"])]


def made(target, values, *more):
    """The arguments make gives the command, one per line: the command is printf, never Klaxon, so nothing runs."""
    env = {key: value for key, value in os.environ.items() if key != "LOOP"}
    done = subprocess.run(
        ["make", "--no-print-directory", "-s", target, *values, *more, "PY=printf '%s\\n'"],
        cwd=REPO, capture_output=True, text=True, env=env, check=True, timeout=60,
    )  # fmt: skip
    return done.stdout.splitlines()


@pytest.mark.parametrize("target, values", MADE)
def test_make_passes_loop_on_as_one_argument_and_only_when_it_is_given(target, values):
    assert "--loop" not in made(target, values)  # LOOP unset: each command's own default, the plain loop or the run's
    given = made(target, values, "LOOP=langgraph")
    assert given[given.index("--loop") + 1] == "langgraph"
    odd = made(target, values, "LOOP=langgraph'; echo pwned")  # through the environment, never as shell text
    assert odd[odd.index("--loop") + 1] == "langgraph'; echo pwned" and "pwned" not in odd


def test_the_commands_take_only_the_loops_this_code_knows_and_default_to_the_plain_loop(capsys):
    for main, argv in [(fault_runs.main, ["run", "poison-message"]), (evaluation.main, ["run"])]:
        with pytest.raises(SystemExit):
            main([*argv, "--loop", "graph"])
        assert "invalid choice: 'graph'" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        loop.main(["replay", "x", "--loop", "graph"])
