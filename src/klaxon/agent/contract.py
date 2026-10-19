"""What Klaxon's loop accepts from the model: one JSON object per reply, checked against a schema.

Each reply is one step, and asks for one of two actions:

    call_tool   {"action": "call_tool", "why": "...", "tool": "service_health", "arguments": {}}
    diagnose    {"action": "diagnose", "why": "...", "diagnosis": {...}}

The diagnosis is the contract a run ends in (PLAN.md): a hypothesis; evidence that cites this run's tool calls
by their ids (c1, c2, ...), each with what it shows; a confidence from 0 to 1; a proposed fix (its kind, its
target, and what to do); the fix's blast radius; and a rollback plan. The fix is only proposed: nothing in the
loop applies it.

A run may also offer write actions (klaxon.agent.catalog, week 4). Then, and only then, the diagnosis may name one,
in fix_action: its name and its arguments, checked against that action's own schema. Naming it runs nothing: code
plans it from the live state, and a person approves it or not, after the run (klaxon.agent.gate). A run that
offers no action gets exactly the schema of week 3, so that its prompts, and the runs recorded with them, stay
the same.

The schema shown to the model is the one enforced. It is JSON Schema, checked by a small validator here for
just the keywords it uses (a test checks that it agrees with the jsonschema library). Beyond the schema, a
reply must be one JSON object and nothing else (one code fence around it is allowed), with no key twice and
no NaN; a tool call's arguments must fit that tool's own input schema; the evidence must cite calls that ran
in this run; and each text of the diagnosis must say something visible. A reply that breaks any of this is
invalid: the loop says why and asks again, a bounded number of times. The problems it lists repeat little of
the reply: field names cleaned and cut short, and call ids only once they fit their pattern.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from klaxon.agent.tools import ToolInputError, check_arguments, clean_text

MAX_REPLY = 20_000  # characters in one reply
MAX_PROBLEMS = 8  # problems listed back to the model at once
CALL_ID = "^c[1-9][0-9]{0,2}$"  # the ids the loop gives tool calls: c1, c2, ...
# What a proposed fix does. The write actions are the ones CLAUDE.md names; each will need a human's approval.
FIX_KINDS = ("rollback", "restart", "replay", "scale", "config_change", "code_change", "data_repair", "none", "other")
FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*)\n[ \t]*```", re.DOTALL)


def _text(most: int, description: str) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": most, "description": description}


@dataclass(frozen=True)
class ActionSpec:
    """A write action a diagnosis may name: its name, what it does, and its arguments as JSON Schema (the subset
    check() reads). Data only: it runs nothing."""

    name: str
    description: str
    input_schema: dict


def reply_schema(tools: Sequence[str], actions: Sequence[str] = ()) -> dict:
    """The JSON Schema of a reply, for these tools, and the names of the actions the run offers: the form the model
    is shown, and the one enforced. With no actions, the schema of week 3, unchanged."""
    why = {"type": "string", "maxLength": 500, "description": "Why this step, in a sentence."}
    call_tool = {
        "type": "object",
        "properties": {
            "action": {"const": "call_tool"},
            "why": why,
            "tool": {"enum": list(tools)},
            "arguments": {"type": "object", "description": "The tool's arguments, as its input schema gives them."},
        },
        "required": ["action", "tool", "arguments"],
        "additionalProperties": False,
    }
    evidence = {
        "type": "object",
        "properties": {
            "call_id": {"type": "string", "pattern": CALL_ID, "description": "A tool call of this run: c1, c2, ..."},
            "shows": _text(500, "What that call's result shows."),
        },
        "required": ["call_id", "shows"],
        "additionalProperties": False,
    }
    fix = {
        "type": "object",
        "properties": {
            "kind": {"enum": list(FIX_KINDS)},
            "target": _text(200, "The service or component the fix acts on."),
            "summary": _text(1000, "What to do."),
        },
        "required": ["kind", "target", "summary"],
        "additionalProperties": False,
    }
    diagnosis = {
        "type": "object",
        "properties": {
            "hypothesis": _text(1000, "The cause, and how it produces the alert."),
            "evidence": {"type": "array", "minItems": 1, "maxItems": 10, "items": evidence},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "0: a guess. 1: certain."},
            "proposed_fix": fix,
            "blast_radius": _text(1000, "What the fix could affect, and what it risks."),
            "rollback_plan": _text(1000, "How to undo the fix if it makes things worse."),
        },
        "required": ["hypothesis", "evidence", "confidence", "proposed_fix", "blast_radius", "rollback_plan"],
        "additionalProperties": False,
    }
    if actions:
        diagnosis["properties"]["fix_action"] = {
            "type": "object",
            "description": "Optional: one action from the list that carries out the fix. It runs only if a human"
            " approves it, after this run. Leave it out when no listed action fits.",
            "properties": {
                "name": {"enum": list(actions)},
                "arguments": {"type": "object", "description": "The action's arguments, as its schema gives them."},
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        }
    diagnose = {
        "type": "object",
        "properties": {"action": {"const": "diagnose"}, "why": why, "diagnosis": diagnosis},
        "required": ["action", "diagnosis"],
        "additionalProperties": False,
    }
    return {"oneOf": [call_tool, diagnose]}


# The validator: the subset of JSON Schema the reply schema uses, and nothing else.

KEYWORDS = frozenset(
    {
        "type",
        "const",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "items",
        "minItems",
        "maxItems",
        "oneOf",
    }
)
ANNOTATIONS = frozenset({"title", "description"})
KINDS = {
    "object": "a JSON object",
    "array": "a list",
    "string": "a string",
    "number": "a number",
    "integer": "a whole number",
    "boolean": "true or false",
    "null": "null",
}


def _is(kind: str, value: object) -> bool:
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "integer":
        return _is("number", value) and (isinstance(value, int) or value.is_integer())
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    return isinstance(value, types[kind])


def _same(value: object, option: object) -> bool:
    """JSON equality for the constants a schema here names: 1 is 1.0, but true is not 1 and "1" is not 1."""
    if _is("number", value) and _is("number", option):  # a number here is never true or false
        return value == option
    return type(value) is type(option) and value == option


def _at(path: str) -> str:
    return f"{path}: " if path else "the reply: "


def _join(path: str, name: str) -> str:
    shown = clean_text(name, 60)  # a field name comes from the reply: shown cleaned and short
    return f"{path}.{shown}" if path else shown


def check(schema: Mapping, value: object, path: str = "") -> list[str]:
    """What is wrong with value against schema, as short problems naming where; [] when it fits."""
    unknown = set(schema) - KEYWORDS - ANNOTATIONS
    if unknown:  # a bug in a schema here, not bad input
        raise AssertionError(f"schema keyword(s) this validator does not support: {sorted(unknown)}")
    if "oneOf" in schema:
        return _one_of(schema["oneOf"], value, path)
    kind = schema.get("type")
    if kind is not None and not _is(kind, value):
        return [f"{_at(path)}must be {KINDS[kind]}"]
    if "const" in schema and not _same(value, schema["const"]):
        return [f"{_at(path)}must be {json.dumps(schema['const'])}"]
    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        return [f"{_at(path)}must be one of: {', '.join(str(option) for option in schema['enum'])}"]
    problems: list[str] = []
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{_at(path)}must not be empty")
        if len(value) > schema.get("maxLength", math.inf):
            problems.append(f"{_at(path)}must be at most {schema['maxLength']} characters (it has {len(value)})")
        # fullmatch: every pattern here is written whole (^...$), and in Python $ also matches before a final "\n".
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            problems.append(f"{_at(path)}must match {schema['pattern']}")
    if _is("number", value):
        if value < schema.get("minimum", -math.inf):
            problems.append(f"{_at(path)}must be at least {schema['minimum']}")
        if value > schema.get("maximum", math.inf):
            problems.append(f"{_at(path)}must be at most {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{_at(path)}must have at least {schema['minItems']} item(s)")
        if len(value) > schema.get("maxItems", math.inf):
            problems.append(f"{_at(path)}must have at most {schema['maxItems']} items (it has {len(value)})")
        if "items" in schema:
            for i, item in enumerate(value[: schema.get("maxItems", len(value))]):
                problems += check(schema["items"], item, f"{path}[{i}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                problems.append(f"{_join(path, name)}: is required")
        for name, item in value.items():
            if name in properties:
                problems += check(properties[name], item, _join(path, name))
            elif schema.get("additionalProperties", True) is False:
                problems.append(f"{_join(path, name)}: is not a field here (the fields: {', '.join(properties)})")
    return problems


def _one_of(branches: Sequence[Mapping], value: object, path: str) -> list[str]:
    """Exactly one branch must fit. Each branch here names its action, so the problems shown are those of the
    branch the reply's action names."""
    found = [check(branch, value, path) for branch in branches]
    fitting = sum(1 for problems in found if not problems)
    if fitting == 1:
        return []
    if fitting > 1:
        return [f"{_at(path)}fits more than one form of reply"]
    actions = [branch.get("properties", {}).get("action", {}).get("const") for branch in branches]
    if isinstance(value, dict):
        for action, problems in zip(actions, found, strict=True):
            if action is not None and _same(value.get("action"), action):
                return problems
        return [f"{_join(path, 'action')}: must be one of: {', '.join(str(a) for a in actions)}"]
    return [f"{_at(path)}must be a JSON object"]


# A reply, read.


@dataclass(frozen=True)
class Evidence:
    """One piece of evidence: a tool call of this run (as it ran), and what its result shows."""

    call_id: str
    tool: str
    arguments: dict
    shows: str


@dataclass(frozen=True)
class ProposedFix:
    kind: str
    target: str
    summary: str


@dataclass(frozen=True)
class FixAction:
    """A write action the diagnosis names, with its arguments, checked against the action's schema. Only named."""

    name: str
    arguments: dict


@dataclass(frozen=True)
class Diagnosis:
    """The contract a run ends in. The fix is proposed, never applied: a human decides."""

    hypothesis: str
    evidence: tuple[Evidence, ...]
    confidence: float
    proposed_fix: ProposedFix
    blast_radius: str
    rollback_plan: str
    fix_action: FixAction | None = None

    def as_dict(self) -> dict:
        found = {
            "hypothesis": self.hypothesis,
            "evidence": [asdict(item) for item in self.evidence],
            "confidence": self.confidence,
            "proposed_fix": asdict(self.proposed_fix) | {"applied": False},  # this loop applies nothing
            "blast_radius": self.blast_radius,
            "rollback_plan": self.rollback_plan,
        }
        if self.fix_action is not None:  # only then: a diagnosis without one reads as in week 3
            found["fix_action"] = asdict(self.fix_action) | {"applied": False}
        return found


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict
    why: str


@dataclass(frozen=True)
class Reading:
    """What the loop read from one reply: the JSON object (None when the reply is not one), the problems that make
    it invalid, and, when there are none, the one action it asks for."""

    value: dict | None
    problems: tuple[str, ...] = ()
    call: ToolCall | None = None
    diagnosis: Diagnosis | None = None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:  # two parsers could read such a reply two ways: refused, not guessed
            raise ValueError(f"the key {clean_text(json.dumps(key), 60)} appears twice in one object")
        seen.add(key)
    return dict(pairs)


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not a JSON number")  # json.loads would otherwise read NaN and Infinity


def _blank_texts(found: dict) -> list[str]:
    """The texts of a diagnosis that say nothing: empty once spaces and characters that do not show are dropped.
    The schema's minLength cannot see them: a zero-width space is one character."""
    texts = [(f"diagnosis.{name}", found[name]) for name in ("hypothesis", "blast_radius", "rollback_plan")]
    texts += [(f"diagnosis.proposed_fix.{name}", found["proposed_fix"][name]) for name in ("target", "summary")]
    texts += [(f"diagnosis.evidence[{i}].shows", item["shows"]) for i, item in enumerate(found["evidence"])]
    return [
        f"{where}: says nothing: it is blank, or holds only characters that do not show"
        for where, text in texts
        if not clean_text(text, len(text)).strip()
    ]


def parse_object(text: str) -> tuple[dict | None, str | None]:
    """The reply as one JSON object, or the problem that stops it being one."""
    if len(text) > MAX_REPLY:
        return None, f"the reply has {len(text):,} characters, over the {MAX_REPLY:,} allowed"
    body = text.strip()
    fenced = FENCE.fullmatch(body)
    if fenced:
        body = fenced.group(1)
    if not body.strip():
        return None, "the reply is empty: it must be one JSON object"
    try:
        value = json.loads(body, object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except RecursionError:
        return None, "the reply is nested too deeply to read"
    except ValueError as exc:  # JSONDecodeError included: its message says where the JSON broke
        return None, f"the reply is not one JSON object and nothing else ({clean_text(str(exc), 200)})"
    if not isinstance(value, dict):
        return None, "the reply must be one JSON object, not a list or a single value"
    return value, None


def read_reply(
    text: str,
    tools: Mapping[str, dict],
    calls: Mapping[str, tuple[str, dict]],
    actions: Mapping[str, dict] | None = None,
) -> Reading:
    """Read one reply. tools: the input schema of each tool the model may call, by name. calls: the tool calls of
    this run so far, as (tool, arguments) by call id. actions: the input schema of each action the run offers, by
    name (none by default)."""
    actions = actions or {}
    value, problem = parse_object(text)
    if value is None:
        return Reading(None, (problem,))
    problems = check(reply_schema(list(tools), list(actions)), value)
    if problems:
        return Reading(value, tuple(problems[:MAX_PROBLEMS]))
    why = value.get("why", "")
    if value["action"] == "call_tool":
        try:
            check_arguments(tools[value["tool"]], value["arguments"])
        except ToolInputError as exc:
            return Reading(value, (f"arguments: {clean_text(str(exc), 300)}",))
        return Reading(value, call=ToolCall(value["tool"], value["arguments"], why))
    cited = value["diagnosis"]["evidence"]
    blank = _blank_texts(value["diagnosis"])
    if blank:
        return Reading(value, tuple(blank[:MAX_PROBLEMS]))
    if not calls:
        return Reading(value, ("diagnosis.evidence: must cite tool calls of this run, and none has run yet",))
    unknown = [
        f"diagnosis.evidence[{i}].call_id: {item['call_id']} is not a tool call of this run (they are: {', '.join(calls)})"
        for i, item in enumerate(cited)
        if item["call_id"] not in calls
    ]
    if unknown:
        return Reading(value, tuple(unknown[:MAX_PROBLEMS]))
    found = value["diagnosis"]
    fix = found["proposed_fix"]
    named = found.get("fix_action")
    if named is not None:  # the schema let it in only when the run offers actions, and only one it offers
        wrong = check(actions[named["name"]], named["arguments"], "diagnosis.fix_action.arguments")
        if wrong:
            return Reading(value, tuple(wrong[:MAX_PROBLEMS]))
    diagnosis = Diagnosis(
        hypothesis=found["hypothesis"],
        evidence=tuple(
            Evidence(item["call_id"], calls[item["call_id"]][0], dict(calls[item["call_id"]][1]), item["shows"])
            for item in cited
        ),
        confidence=float(found["confidence"]),
        proposed_fix=ProposedFix(fix["kind"], fix["target"], fix["summary"]),
        blast_radius=found["blast_radius"],
        rollback_plan=found["rollback_plan"],
        fix_action=None if named is None else FixAction(named["name"], dict(named["arguments"])),
    )
    return Reading(value, diagnosis=diagnosis)
