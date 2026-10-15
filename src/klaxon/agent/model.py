"""How Klaxon's loop asks a model for its next step: one interface, the offline adapters, and the recording.

The loop (week 3, item 3) owns the conversation and runs every tool itself, through its MCP server. An
adapter only answers: given a system prompt and the conversation so far, ask() returns the model's next
reply as text, with the model id the answer reports, the usage, the time it took and the raw response.

    ScriptedModel   offline tests: replies from a script, and a record of everything it was asked
    ClaudeCode      live: Claude through Claude Code in headless mode (in klaxon.agent.claude_code)
    ReplayModel     regression tests: replies recorded from live calls, each served only for the exact
                    prompt it answered

A Recording keeps every live call as it happened: the date, the model id the response reports, a hash of
the prompt, the text, the usage and the raw output, labeled as a recorded response. CLAUDE.md: any Claude
response shown in a deliverable must be one actually received, saved with its date and model. The prompts
are kept too, each part once, so a reader can see exactly what each reply answered.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

ROLES = ("user", "assistant")
FORMAT = 1  # of a recording's records
LABEL = (
    "Recorded responses: each one received from Claude through Claude Code in headless mode (claude -p) on"
    " Aravind's seat, and saved before Klaxon used it. Not a simulation. The text is exactly as received; in the"
    " raw output, this Mac's home folder is written as ~."
)


class ModelError(RuntimeError):
    """No reply the loop may use: the call failed or timed out, or Klaxon refused what came back."""


class ModelTimeout(ModelError):
    """No reply within the time allowed."""


class ModelUnsafe(ModelError):
    """The model's runtime could have run something of its own: a tool, an MCP server, another folder."""


class ModelMismatch(ModelError):
    """The reply came from another model than the one asked for."""


class ScriptExhausted(ModelError):
    """A scripted model was asked more often than its script has replies."""


class NotRecorded(ModelError):
    """A replay has no recorded reply for this exact prompt."""


@dataclass(frozen=True)
class Message:
    """One turn of the conversation."""

    role: str  # "user" or "assistant"
    content: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"a turn's role is user or assistant, not {self.role!r}")
        if not isinstance(self.content, str):
            raise TypeError("a turn's content is text")


def check_conversation(messages: Sequence[Message]) -> tuple[Message, ...]:
    """The conversation, checked: it starts and ends with the user, and the roles alternate, as in the Messages
    API. Ending with the user means the next turn is the model's."""
    turns = tuple(messages)
    if not turns:
        raise ValueError("the conversation is empty")
    for i, turn in enumerate(turns):
        if not isinstance(turn, Message):
            raise TypeError("each turn is a Message")
        if turn.role != ROLES[i % 2]:
            raise ValueError(f"turn {i + 1} should be the {ROLES[i % 2]}'s: turns start with the user and alternate")
    if turns[-1].role != "user":
        raise ValueError("the conversation must end with the user's turn: the next one is the model's")
    return turns


def _sha256(value: object) -> str:
    # surrogatepass: a reply can carry a lone surrogate (JSON allows "\ud800"); it must still hash, not crash.
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def prompt_sha256(system: str, messages: Sequence[Message]) -> str:
    """SHA-256 of what the model is asked: the system prompt and every turn, in order, before an adapter frames
    them. The same prompt always gives the same hash, whichever adapter answers it."""
    return _sha256({"system": system, "messages": [[m.role, m.content] for m in messages]})


def text_sha256(text: str) -> str:
    """SHA-256 of one text, as UTF-8: a system prompt, a turn, a reply, a tool result. The recording and the
    trace both name texts this way, so one can be matched with the other."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def finite(value: object) -> float | None:
    """A non-negative, finite number as a float; None for anything else (a NaN would break a JSON record)."""
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


@dataclass(frozen=True)
class Usage:
    """Tokens, as the response reports them; None where it does not say (a scripted reply says nothing)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    @classmethod
    def from_api(cls, usage: object) -> Usage:
        """From the Messages API's usage object, which Claude Code passes on: whole, non-negative counts only."""
        if not isinstance(usage, dict):
            return cls()
        return cls(
            input_tokens=_count(usage.get("input_tokens")),
            output_tokens=_count(usage.get("output_tokens")),
            cache_read_tokens=_count(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_count(usage.get("cache_creation_input_tokens")),
        )

    @classmethod
    def from_record(cls, usage: object) -> Usage:
        """From a recording, where each count was saved under its own name."""
        if not isinstance(usage, dict):
            return cls()
        return cls(**{f.name: _count(usage.get(f.name)) for f in fields(cls)})

    @property
    def total_input_tokens(self) -> int | None:
        """Everything the model read: new input, plus what it read from or wrote to the prompt cache."""
        parts = (self.input_tokens, self.cache_read_tokens, self.cache_write_tokens)
        return None if all(p is None for p in parts) else sum(p or 0 for p in parts)


@dataclass(frozen=True)
class Reply:
    """The model's next turn, and what is known about how it came."""

    text: str
    model: str  # the model id the response reports
    usage: Usage
    seconds: float  # how long the call took
    source: str  # "scripted", "claude-code" or "replay"
    prompt_sha256: str
    received_at: str  # UTC, ISO 8601; for a replay, when the recorded reply was first received
    cost_usd: float | None = None  # Claude Code's estimate at API prices; a seat is not billed per call
    raw: object = field(default=None, repr=False, compare=False)


class Model(Protocol):
    """What the loop needs from an adapter."""

    source: str

    def ask(self, system: str, messages: Sequence[Message]) -> Reply: ...


Entry = str | BaseException | Callable[[str, tuple[Message, ...]], str]


class ScriptedModel:
    """Offline: each call takes the next entry of a script. An entry is the reply's text, an exception to raise
    (a timeout, an outage), or a function of the system prompt and the conversation that returns the text.
    Every call is kept in .calls, so a test can check exactly what the model was shown."""

    source = "scripted"

    def __init__(self, script: Iterable[Entry], model: str = "scripted") -> None:
        self.script = list(script)
        self.model = model
        self.calls: list[tuple[str, tuple[Message, ...]]] = []

    def ask(self, system: str, messages: Sequence[Message]) -> Reply:
        turns = check_conversation(messages)
        self.calls.append((system, turns))
        index = len(self.calls) - 1
        if index >= len(self.script):
            raise ScriptExhausted(f"the script has {len(self.script)} replies, and this is call {index + 1}")
        entry = self.script[index]
        if isinstance(entry, BaseException):
            raise entry
        text = entry(system, turns) if callable(entry) else entry
        if not isinstance(text, str):
            raise TypeError("a scripted reply is text")
        return Reply(
            text=text,
            model=self.model,
            usage=Usage(),
            seconds=0.0,
            source=self.source,
            prompt_sha256=prompt_sha256(system, turns),
            received_at=utc_now(),
            raw={"script_entry": index},
        )


def read_records(path: Path | str) -> list[dict]:
    """A JSON Lines file of records (a recording or a trace), one JSON object per line."""
    records = []
    with open(path, encoding="utf-8") as lines:
        for number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
            except ValueError:
                raise ValueError(f"line {number} of {Path(path).name} is not JSON") from None
            if not isinstance(record, dict):
                raise ValueError(f"line {number} of {Path(path).name} is not a record")
            records.append(record)
    return records


LIVE = ("claude-code",)  # the sources whose replies come from a model, live; a recording keeps only these


class Recording:
    """Every live call, as it happened: one JSON line each, appended to a file that is never rewritten.

    The records, in the order written:
        header        each time the file is opened for writing: the label, and where and from what code the
                      calls came
        prompt_part   a system prompt or a turn, stored once under its SHA-256, the first time a call uses it
        response      a reply as received: its date, the model id it reports, the prompt's hash and parts,
                      the text, the usage, and the adapter's raw output; with accepted false, the error and
                      why, when Klaxon refused it (another model answered, for example)
        failure       a call that gave no usable reply: the error, why, and how long it took
    """

    def __init__(self, path: Path | str, header: dict | None = None) -> None:
        self.path = Path(path)
        existing = read_records(self.path) if self.path.exists() else []
        self._stored = {r.get("sha256") for r in existing if r.get("kind") == "prompt_part"}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        opened = {"kind": "header", "format": FORMAT, "label": LABEL, "opened_at": utc_now()}
        self._write([opened | (header or {})])

    def _write(self, records: list[dict]) -> None:
        """Append records, all or none: a write that fails is cut back off, so no half line is left behind."""
        # ASCII only: every other character is escaped, so no line separator inside a text can split a record.
        data = "".join(
            json.dumps(r, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n" for r in records
        ).encode("ascii")
        with open(self.path, "ab") as out:
            start = out.seek(0, 2)
            try:
                out.write(data)
                out.flush()
            except BaseException:
                out.truncate(start)
                raise
        self._stored.update(r["sha256"] for r in records if r["kind"] == "prompt_part")  # only once on disk

    def _part(self, content: str, new_parts: list[dict]) -> str:
        """A text's hash; the text itself goes into new_parts if this file does not hold it yet."""
        digest = text_sha256(content)
        if digest not in self._stored and all(part["sha256"] != digest for part in new_parts):
            new_parts.append({"kind": "prompt_part", "sha256": digest, "characters": len(content), "content": content})
        return digest

    def _prompt(self, system: str, messages: Sequence[Message], new_parts: list[dict]) -> dict:
        """The prompt as hashes: its own, the system prompt's, and each turn's with its role."""
        return {
            "sha256": prompt_sha256(system, messages),
            "characters": len(system) + sum(len(m.content) for m in messages),
            "system": self._part(system, new_parts),
            "turns": [[m.role, self._part(m.content, new_parts)] for m in messages],
        }

    def response(
        self,
        system: str,
        messages: Sequence[Message],
        reply: Reply,
        details: dict,
        refused: ModelError | None = None,
    ) -> dict:
        """Record a reply as received. refused: the error Klaxon raised instead of using it, if it did."""
        if reply.source not in LIVE:
            raise ValueError(f"a recording keeps live replies only, not {reply.source!r} ones")
        new_parts: list[dict] = []
        record = {
            "kind": "response",
            "label": "recorded response, as received",
            "received_at": reply.received_at,
            "model": reply.model,
            "source": reply.source,
            "prompt_sha256": reply.prompt_sha256,
            "prompt": self._prompt(system, messages, new_parts),
            "text": reply.text,
            "usage": asdict(reply.usage),
            "cost_usd": reply.cost_usd,
            "seconds": reply.seconds,
            "accepted": refused is None,
            **({"error": type(refused).__name__, "refused": str(refused)} if refused is not None else {}),
            "details": details,
            "raw": reply.raw,
        }
        self._write([*new_parts, record])
        return record

    def failure(
        self, system: str, messages: Sequence[Message], error: ModelError, seconds: float, details: dict, raw: object
    ) -> dict:
        """Record a call that gave no usable reply."""
        new_parts: list[dict] = []
        record = {
            "kind": "failure",
            "label": "a live call that gave no usable reply",
            "at": utc_now(),
            "error": type(error).__name__,
            "reason": str(error),
            "prompt": self._prompt(system, messages, new_parts),
            "seconds": seconds,
            "details": details,
            "raw": raw,
        }
        self._write([*new_parts, record])
        return record


def rebuild_prompt(records: list[dict], record: dict) -> tuple[str, tuple[Message, ...]]:
    """The exact prompt a recorded response or failure answered, from the recording's stored parts. Refuses a
    recording whose parts or prompt do not match their hashes: it was damaged or edited."""
    parts = {r.get("sha256"): r.get("content") for r in records if r.get("kind") == "prompt_part"}

    def text(digest: object) -> str:
        content = parts.get(digest)
        if not isinstance(content, str) or text_sha256(content) != digest:
            raise ValueError(f"the recording's prompt part {str(digest)[:12]} is missing or does not match its hash")
        return content

    prompt = record["prompt"]
    system = text(prompt["system"])
    messages = tuple(Message(role, text(digest)) for role, digest in prompt["turns"])
    claimed = {prompt["sha256"], record.get("prompt_sha256", prompt["sha256"])}
    if claimed != {prompt_sha256(system, messages)}:
        raise ValueError("a recorded prompt does not match its hash")
    return system, messages


ERRORS = {kind.__name__: kind for kind in (ModelError, ModelTimeout, ModelUnsafe, ModelMismatch)}


class ReplayModel:
    """Regression tests: what live calls gave, served again for the exact prompt each one answered, in the order
    recorded. A recorded reply comes back as a reply; a recorded failure or refusal comes back as the error it
    was, so a run that retried after a timeout retries again. Nothing is called. A changed prompt gets nothing:
    record a new run. The match is on the prompt itself, not on how the live adapter framed it."""

    source = "replay"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        records = read_records(self.path)
        self._waiting: dict[str, deque[dict]] = {}
        for record in records:
            if record.get("kind") not in ("response", "failure"):
                continue
            rebuild_prompt(records, record)  # a damaged or edited recording is refused here, not served
            if record["kind"] == "response" and record.get("accepted") is True:
                kinds = (record.get("text"), record.get("model"), record.get("received_at"))
                if not all(isinstance(value, str) for value in kinds) or finite(record.get("seconds")) is None:
                    raise ValueError(
                        "a recorded response lacks its text, model, date or time: it was damaged or edited"
                    )
            self._waiting.setdefault(record["prompt"]["sha256"], deque()).append(record)
        self.served = 0

    @property
    def unused(self) -> int:
        return sum(len(waiting) for waiting in self._waiting.values())

    def ask(self, system: str, messages: Sequence[Message]) -> Reply:
        turns = check_conversation(messages)
        digest = prompt_sha256(system, turns)
        waiting = self._waiting.get(digest)
        if not waiting:
            raise NotRecorded(
                f"{self.path.name} has nothing recorded left for this prompt (sha256 {digest[:12]}): the prompt"
                " changed since the recording, or this run went further than the recorded one"
            )
        record = waiting.popleft()
        self.served += 1
        if record["kind"] == "failure" or record.get("accepted") is not True:
            error = ERRORS.get(str(record.get("error")), ModelError)
            raise error(f"replayed from {self.path.name}: {record.get('reason') or record.get('refused')}")
        return Reply(
            text=record["text"],
            model=record["model"],
            usage=Usage.from_record(record.get("usage")),
            seconds=finite(record["seconds"]),
            source=self.source,
            prompt_sha256=digest,
            received_at=record["received_at"],
            cost_usd=finite(record.get("cost_usd")),
            raw={"replayed_from": self.path.name, "recorded_from": record.get("source")},
        )
