"""Klaxon's trace recorder: one JSON line per step of a run, so a run can be read, audited and graded after.

The loop (week 3, item 3) writes it. A trace file holds, in order:

    start   the run: its goal, the model and where its replies come from (live, scripted or replayed),
            the system prompt and its hash, the tools, the budgets
    step    one per step: what the model saw (the prompt's hash and size, and the turns new since the last
            step), what it replied and proposed, whether that was allowed and why not, what ran (the tool
            and its arguments), what came back, and the step's timing and usage
    stop    why the run stopped, and its totals

Everything a model or a tool wrote is untrusted, so each text is cleaned the way tool output is (hidden
characters dropped, the home folder written as ~) and capped. Each keeps its full length and SHA-256, so
it can be matched with the exact text in the model recording. Nothing runs unless allowed: a step that
says a tool ran without being allowed is refused. Each record is flushed as it is written, so a crashed
run still leaves a readable trace, closed with a "crashed" stop record. A trace file is never overwritten.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, fields
from pathlib import Path

from klaxon.agent.model import Message, Reply, Usage, prompt_sha256, read_records, text_sha256, utc_now
from klaxon.agent.tools import clean, clean_text

FORMAT = 1
MAX_TEXT = 8_000  # characters of a reply or a tool result kept in the trace; the recording keeps the reply whole
MAX_SYSTEM = 20_000  # the system prompt, once, in the start record
MAX_SHORT = 300  # a reason, an error, a detail

STOP_REASONS = {
    "diagnosed": "the model gave a diagnosis that passed validation",
    "step_budget": "the run used all its steps",
    "tool_call_budget": "the run used all its tool calls",
    "time_budget": "the run used all its time",
    "invalid_replies": "too many replies in a row could not be used",
    "model_error": "the model gave no usable reply: an error, a timeout, or a reply Klaxon refused",
    "interrupted": "someone stopped the run",
    "crashed": "the run ended on an unexpected error, or without saying why it stopped",
}
SOURCES = {
    "scripted": "scripted (an offline test): no model was called",
    "claude-code": "live, from Claude through Claude Code in headless mode; each one is also in the model recording",
    "replay": "replayed from a recording of earlier live replies: no model was called",
}


class TraceError(RuntimeError):
    """The loop used the recorder out of order, or recorded something that cannot have happened."""


def text_field(value: str, limit: int = MAX_TEXT) -> dict:
    """A text from a model or a tool, for the trace: cleaned and capped, with its full length and SHA-256."""
    if not isinstance(value, str):
        raise TraceError(f"a goal, system prompt, reply or tool result is text, not {type(value).__name__}")
    kept = clean_text(value, len(value))  # cleaned, not cut: cleaning only ever removes characters
    shown = kept if len(kept) <= limit else f"{kept[:limit]}...[{len(kept) - limit} more characters]"
    return {"characters": len(value), "sha256": text_sha256(value), "cut": len(kept) > limit, "text": shown}


class Trace:
    """One run's trace, a JSON Lines file written as the run goes. Use it as a context manager:

        with Trace(path) as trace:
            trace.start(...)
            trace.step(...)      # once per step
            trace.stop("diagnosed", ...)

    If the block ends without stop(), the trace says why: interrupted, or crashed (with the error's type).
    """

    def __init__(
        self, path: Path | str, run_id: str | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "x", encoding="utf-8")  # "x": an earlier trace is never overwritten
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._clock = clock
        self._started = self._last = clock()
        self._seq = 0
        self._state = "new"  # then "running", then "stopped"
        self._seen: list[str] = []  # the hashes of the turns the model saw at the last step
        self.steps = 0
        self.tool_calls = 0
        self._model_seconds = 0.0
        self._tool_seconds = 0.0
        self._usage = {f.name: 0 for f in fields(Usage)}

    def _write(self, kind: str, body: dict) -> dict:
        now = self._clock()
        record = {"trace": "klaxon", "format": FORMAT, "run_id": self.run_id, "seq": self._seq, "kind": kind}
        record |= {"at": utc_now(), "t": round(now - self._started, 3)} | body
        # ASCII only: every other character is escaped, so no line separator inside a text can split a record.
        self._file.write(json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n")
        self._file.flush()
        self._seq += 1
        self._last = now
        return record

    def start(
        self,
        *,
        goal: str,
        system: str,
        model: str,
        source: str,
        tools: Sequence[str],
        budgets: Mapping[str, object],
        **extra: object,
    ) -> dict:
        """The run's first record: what it is for, and what it may use."""
        if self._state != "new":
            raise TraceError("a trace starts once, before its first step")
        if source not in SOURCES:
            raise TraceError(f"unknown reply source {source!r}: one of {', '.join(SOURCES)}")
        self._state = "running"
        body = {
            "label": f"Trace of a Klaxon run. Model replies: {SOURCES[source]}.",
            "goal": text_field(goal),
            "model": clean_text(model, MAX_SHORT),
            "source": source,
            "system": text_field(system, MAX_SYSTEM),
            "tools": [clean_text(str(name), 100) for name in tools],
            "budgets": clean(dict(budgets)),
        }
        if extra:
            body["extra"] = clean(extra)
        return self._write("start", body)

    def step(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        reply: Reply | None = None,
        error: str | None = None,
        proposed: object = None,
        allowed: bool | None = None,
        why: str | None = None,
        ran: Mapping[str, object] | None = None,
        came_back: str | None = None,
        came_back_error: bool = False,
        tool_seconds: float | None = None,
    ) -> dict:
        """One step. messages: the conversation the model saw. reply, or error when it gave none. proposed: what
        the loop read from the reply. allowed and why: the loop's decision. ran: the tool and its arguments
        that ran (only if allowed). came_back: the tool's result, as the model will see it."""
        if self._state != "running":
            raise TraceError("steps go between start and stop")
        if ran is not None and allowed is not True:
            raise TraceError("a tool ran without being allowed")
        if allowed is not None and proposed is None:
            raise TraceError("an allow or refuse decision about nothing proposed")
        if came_back is not None and ran is None:
            raise TraceError("a result came back from a tool that did not run")
        self.steps += 1
        body: dict = {"step": self.steps, "saw": self._saw(system, messages)}
        if reply is not None:
            body["reply"] = {
                "source": reply.source,
                "model": clean_text(reply.model, 100),
                "received_at": clean_text(reply.received_at, 40),
                "seconds": reply.seconds,
                "usage": asdict(reply.usage),
                "cost_usd": reply.cost_usd,
                **text_field(reply.text),
            }
            self._model_seconds += reply.seconds
            for name, count in asdict(reply.usage).items():
                self._usage[name] += count or 0
        if error is not None:
            body["error"] = clean_text(error, MAX_SHORT)
        body["proposed"] = clean(proposed)
        body["allowed"] = allowed
        if why is not None:
            body["why"] = clean_text(why, MAX_SHORT)
        if ran is not None:
            body["ran"] = clean(dict(ran))
            self.tool_calls += 1
        if came_back is not None:
            body["came_back"] = {"is_error": bool(came_back_error), **text_field(came_back)}
        if tool_seconds is not None:
            body["tool_seconds"] = round(tool_seconds, 3)
            self._tool_seconds += tool_seconds
        body["step_seconds"] = round(self._clock() - self._last, 3)
        return self._write("step", body)

    def _saw(self, system: str, messages: Sequence[Message]) -> dict:
        """What the model saw, as hashes and sizes: the turns since the last step, and if earlier ones changed."""
        hashes = [text_sha256(m.content) for m in messages]
        same = 0
        while same < min(len(hashes), len(self._seen)) and hashes[same] == self._seen[same]:
            same += 1
        saw = {
            "prompt_sha256": prompt_sha256(system, messages),
            "system_sha256": text_sha256(system),
            "characters": len(system) + sum(len(m.content) for m in messages),
            "turns": len(messages),
            "new_turns": [
                {"role": m.role, "characters": len(m.content), "sha256": digest}
                for m, digest in zip(messages[same:], hashes[same:], strict=True)
            ],
        }
        if same < len(self._seen):
            saw["earlier_turns_changed"] = True  # the loop rewrote what the model had seen before
        self._seen = hashes
        return saw

    def stop(self, reason: str, detail: str = "", diagnosis: object = None) -> dict:
        """The run's last record: why it stopped, and its totals."""
        if self._state != "running":
            raise TraceError("a run stops once, after it starts")
        if reason not in STOP_REASONS:
            raise TraceError(f"unknown stop reason {reason!r}: one of {', '.join(STOP_REASONS)}")
        self._state = "stopped"
        body = {"reason": reason, "meaning": STOP_REASONS[reason], "detail": clean_text(detail, MAX_SHORT)}
        if diagnosis is not None:
            body["diagnosis"] = clean(diagnosis)
        body["totals"] = {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "seconds": round(self._clock() - self._started, 3),
            "model_seconds": round(self._model_seconds, 3),
            "tool_seconds": round(self._tool_seconds, 3),
            "usage": dict(self._usage),
        }
        record = self._write("stop", body)
        self._file.close()
        return record

    def close(self, error: BaseException | None = None) -> None:
        """End the trace. A run still going is stopped here, with the reason it ended: interrupted or crashed."""
        if self._state == "running":
            if isinstance(error, KeyboardInterrupt):
                self.stop("interrupted", "stopped from the keyboard")
            elif error is not None:
                self.stop("crashed", f"{type(error).__name__}: {error}")
            else:
                self.stop("crashed", "the run ended without a stop reason")
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> Trace:
        return self

    def __exit__(self, kind: object, error: BaseException | None, traceback: object) -> None:
        self.close(error)


def read_trace(path: Path | str) -> list[dict]:
    """A trace's records, in order."""
    return read_records(path)
