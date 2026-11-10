"""Klaxon's loop as a LangGraph graph (week 6, item 2): the same loop, with its control flow in LangGraph.

Aravind's choice (2026-11-09): "Same loop, as a graph". Only the control flow moves: which part of a run comes next,
and when the run stops. Each node runs the plain loop's own code for its part (klaxon.agent.loop.Investigation, whose
methods both loops call), so the prompt, the reply format, the tools, the budgets, the checks, the trace and the
recordings are the plain loop's. So is the model adapter: Claude Code on his seat, never a LangChain model class. A run
on either loop is graded the same way, and a run made on one loop replays exactly on the other.

The graph (python -m klaxon.agent.graph draw prints LangGraph's own drawing of it, in Mermaid):

    open_tools      the tools opened and checked, the system prompt, the trace's start, the alert as the first turn
    budgets         before each step: the run's time, then its steps
    ask_model       one model call, within the time left; a call that failed is asked again, or the run stops
    read_reply      the reply read: a tool call, a diagnosis, or what was wrong with it (sent back, or the run stops)
    rollback_check  a diagnosis meets the run's checks: taken, and the run stops; or sent back once
    call_tool       one read-only tool, within the budgets: its result is the model's next turn

Each edge is a decision. Every stop rule is an edge to END, from the node whose part stopped the run; every other edge
goes on, to the next part of the step or to the budgets before the next step. The graph's state is the step in hand
(what the model saw, its reply, what the loop read from it, and the result once the run stops). The run's own state,
the conversation, the counts and the calls, is the plain loop's State, changed only by the plain loop's code.

LangGraph's checkpointer and interrupt are not used (his choice): the approval gate stays shared code, after the loop
(klaxon.agent.gate). LangGraph runs a lone task in the caller's thread, so an interrupt stops the step it is in, as in
the plain loop. LangGraph's recursion limit is set from the budgets: the most nodes a run can take, so a bug that
looped without using a step would end the run as a crash, never run on.

LangSmith, to which LangGraph's libraries can send traces, stays off: this loop refuses to run while the environment,
or LangSmith's own switch, turns its tracing on, so nothing of a run leaves this Mac. LangGraph is an optional extra
(.[graph]; it and every library it brings are pinned in requirements/graph.lock.txt), and the plain loop never imports
this module.

Run: python -m klaxon.agent.graph draw    the graph in Mermaid, as LangGraph draws it; calls nothing
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import version
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith.utils import tracing_is_enabled

from klaxon.agent.contract import Reading
from klaxon.agent.loop import LANGGRAPH, Budgets, Investigation, LoopUnavailable, RunResult
from klaxon.agent.model import Message, ModelError, Reply

VERSION = version("langgraph")
# The environment's switches for LangSmith's tracing, as langsmith.utils reads them ("true" turns it on).
TRACING = ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING")
# The most nodes one step runs: the budgets, ask_model, read_reply, then call_tool or rollback_check.
NODE_RUNS_PER_STEP = 4


class Turn(TypedDict):
    """What passes from node to node: the step in hand. The run's own state is the plain loop's (loop.State)."""

    seen: list[Message]  # the conversation the model saw at this step
    reply: Reply | None  # its reply; None when the call gave none
    reading: Reading | None  # what the loop read from the reply
    result: RunResult | None  # how the run stopped, once a part has stopped it


def first_turn() -> Turn:
    return {"seen": [], "reply": None, "reading": None, "result": None}


# The edges: each node's decision, and where each answer goes. Each stop rule is a labeled edge to END.

STOPPED = {
    "open_tools": "tools refused",
    "budgets": "budget used",
    "ask_model": "no reply: stop",
    "read_reply": "invalid: stop",
    "rollback_check": "diagnosed",
    "call_tool": "refused or failed",
}


def after_open(turn: Turn) -> str:
    return STOPPED["open_tools"] if turn["result"] is not None else "first step"


def after_budgets(turn: Turn) -> str:
    return STOPPED["budgets"] if turn["result"] is not None else "a step left"


def after_ask(turn: Turn) -> str:
    if turn["result"] is not None:
        return STOPPED["ask_model"]
    return "no reply: again" if turn["reply"] is None else "a reply"


def after_read(turn: Turn) -> str:
    if turn["result"] is not None:
        return STOPPED["read_reply"]
    reading = turn["reading"]
    if reading.problems:
        return "invalid: again"
    return "a diagnosis" if reading.diagnosis is not None else "a tool call"


def after_check(turn: Turn) -> str:
    return STOPPED["rollback_check"] if turn["result"] is not None else "sent back"


def after_tool(turn: Turn) -> str:
    return STOPPED["call_tool"] if turn["result"] is not None else "a result"


EDGES: dict[str, tuple[Callable[[Turn], str], dict[str, str]]] = {
    "open_tools": (after_open, {STOPPED["open_tools"]: END, "first step": "budgets"}),
    "budgets": (after_budgets, {STOPPED["budgets"]: END, "a step left": "ask_model"}),
    "ask_model": (
        after_ask,
        {STOPPED["ask_model"]: END, "no reply: again": "budgets", "a reply": "read_reply"},
    ),
    "read_reply": (
        after_read,
        {
            STOPPED["read_reply"]: END,
            "invalid: again": "budgets",
            "a diagnosis": "rollback_check",
            "a tool call": "call_tool",
        },
    ),
    "rollback_check": (after_check, {STOPPED["rollback_check"]: END, "sent back": "budgets"}),
    "call_tool": (after_tool, {STOPPED["call_tool"]: END, "a result": "budgets"}),
}
NODES = tuple(EDGES)


def build(nodes: Mapping[str, Callable[[Turn], dict]]):
    """The graph, compiled, with these functions as its nodes: one for each name in NODES. No checkpointer."""
    graph = StateGraph(Turn)
    for name in NODES:
        graph.add_node(name, nodes[name])
    graph.add_edge(START, "open_tools")
    for name, (decide, where) in EDGES.items():
        graph.add_conditional_edges(name, decide, where)
    return graph.compile()


def recursion_limit(budgets: Budgets) -> int:
    """LangGraph's limit, from the run's budgets: the most nodes a run can take (the tools opened, then each step's
    nodes, the last of which stops the run), and one more, as LangGraph counts the run's input as a step of its own."""
    return 1 + NODE_RUNS_PER_STEP * budgets.steps + 1


def tracing_switched_on() -> list[str]:
    """What would turn LangSmith's tracing on: the environment's switches, read now, and LangSmith's own reading."""
    on = [name for name in TRACING if os.environ.get(name, "").strip().lower() == "true"]
    if not on and tracing_is_enabled():
        on.append("LangSmith's own switch (tracing_is_enabled)")
    return on


def check_ready() -> None:
    """Refuses (LoopUnavailable) when LangSmith's tracing is switched on: it would send the run off this Mac."""
    on = tracing_switched_on()
    if on:
        raise LoopUnavailable(
            f"LangSmith tracing is switched on ({', '.join(on)}), and would send the run off this Mac: switch it off"
            " to use the LangGraph loop"
        )


class GraphInvestigation(Investigation):
    """One run, its parts run by a LangGraph graph: the plain loop's code for each part, LangGraph for what comes
    next."""

    kind = LANGGRAPH

    @classmethod
    def about(cls) -> dict:
        """What the trace says of the loop that ran the run: its kind, and LangGraph's version."""
        return {"kind": cls.kind, "langgraph_version": VERSION}

    def run(self) -> RunResult:
        found = self.graph().invoke(first_turn(), config={"recursion_limit": recursion_limit(self.budgets)})
        return found["result"]

    def graph(self):
        """This run's graph, its nodes bound to this run."""
        return build(
            {
                "open_tools": self._open_tools,
                "budgets": self._budgets,
                "ask_model": self._ask_model,
                "read_reply": self._read_reply,
                "rollback_check": self._rollback_check,
                "call_tool": self._call_tool,
            }
        )

    # The nodes: each one part of a run, by the plain loop's code; each returns what changed in the step in hand.

    def _open_tools(self, turn: Turn) -> dict:
        return {"result": self._open()}

    def _budgets(self, turn: Turn) -> dict:
        return {"result": self._out_of_budget()}

    def _ask_model(self, turn: Turn) -> dict:
        seen = list(self.state.conversation)
        try:
            reply = self._ask(seen)
        except ModelError as exc:
            return {"seen": seen, "reply": None, "reading": None, "result": self._failed(seen, exc)}
        return {"seen": seen, "reply": reply, "reading": None, "result": None}

    def _read_reply(self, turn: Turn) -> dict:
        reading = self._read(turn["reply"])
        stopped = self._invalid(turn["seen"], turn["reply"], reading) if reading.problems else None
        return {"reading": reading, "result": stopped}

    def _rollback_check(self, turn: Turn) -> dict:
        return {"result": self._diagnosed(turn["seen"], turn["reply"], turn["reading"])}

    def _call_tool(self, turn: Turn) -> dict:
        return {"result": self._tool(turn["seen"], turn["reply"], turn["reading"])}


def _never(turn: Turn) -> dict:
    raise RuntimeError("a drawing runs no node")


def draw() -> str:
    """The graph in Mermaid, as LangGraph draws it from the graph itself: the same for every run."""
    return build(dict.fromkeys(NODES, _never)).get_graph().draw_mermaid()


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["draw"]:
        print("usage: python -m klaxon.agent.graph draw", file=sys.stderr)
        return 2
    print(draw(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
