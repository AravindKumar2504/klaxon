"""How a diagnosis compares with the cause a fault records: a rule, written before Klaxon's first runs on the faults.

klaxon.faults records each fault's cause in plain words. Here each cause is cut into three elements a correct
diagnosis names: where it is (the ledger consumer, say), what is wrong there (its dedupe is gone), and how that
produces the symptom (the provider sends some webhooks again). The alert's own words can name one element at most
(the lag alert names the ledger group; the latency alert says "slow"), never enough for "partly": a test checks it
for every alert the faults raise. Each element is a few regular expressions, matched without regard to case. The rule
reads the diagnosis's hypothesis, the field the diagnosis contract keeps for "the cause, and how it produces the
alert", and counts the required elements it names:

    names the cause    all of them
    partly             at least half of them
    misses the cause   fewer than half

Optional elements (the deploy that brought the bad setting) are reported, and do not change the verdict. The rule
also says whether the proposed fix is of a kind, and on a target, that fit the fault's remedy.

A rule reads words, not meaning: "a deploy is unlikely" names a deploy, and the right cause put in other words can be
missed. So the evidence shows its verdict next to the hypothesis and the recorded cause, for a person to check, and
it is always labeled as a rule's. The independent review found plausible wrong causes it scores as right (a database
outage that makes the ledger retry; Redis losing its keys; a slow database) and right ones it scores as partly (an
amount "not positive integer cents"; a dedupe "check turned off"): words from the system prompt and the services'
own log lines match elements. tests/test_grading.py keeps those sentences as known limits.

Version 1, below, is the rubric committed in 95a64ab, before the first live runs on the faults (2026-10-19 UTC), and
it graded them. It stays as it was: changing it after seeing results would undo that. Every comparison records the
rubric's version and the fingerprint of its patterns. A test pins each version's fingerprint, and the tests of its
verdicts pin how it counts (the hypothesis only, case ignored, all elements for "names", half for "partly"), each with
a mutant. So a change to either fails a test: a later rubric must then come as a new version.

Version 2 (week 4, item 3), committed in 8e58e7f before any live run on fault 3 with the circuit breaker: the payments
API now fails fast when the provider keeps failing, on by default, so fault 3 records a new cause (klaxon.faults keeps
version 1's). Its third element becomes the breaker failing fast, and the fixes that fit are none and other (escalate
to the provider): there is nothing to change on our side. Faults 1 and 2 are graded as in version 1. New comparisons
use the latest version; a saved run is graded again with the version that graded it (klaxon.fault_runs grade).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from klaxon import faults
from klaxon.agent.tools import clean_text

RUBRIC_VERSION = 2  # the version new comparisons use; each comparison records its own
HOW = "keyword patterns per element of the fault's recorded cause. Not a person and not a model: it reads words, not meaning."
BY = {
    1: (
        "a rule (klaxon.grading, rubric version 1, committed in 95a64ab before the first live runs on the faults):"
        f" {HOW}"
    ),
    2: (
        "a rule (klaxon.grading, rubric version 2, committed in 8e58e7f before any live run on fault 3 with the circuit"
        f" breaker): {HOW}"
    ),
}


@dataclass(frozen=True)
class Element:
    name: str  # what it stands for, in a few words
    patterns: tuple[str, ...]  # regular expressions; one match is enough
    required: bool = True


@dataclass(frozen=True)
class Rubric:
    elements: tuple[Element, ...]
    fix_kinds: tuple[str, ...]  # the kinds of proposed fix that fit the fault's remedy
    fix_target: str  # a regular expression the fix's target must match


LEDGER = Element("where: the ledger consumer", (r"\bledger\b",))
DEPLOY = (r"deploy", r"roll(ed)?[ -]?out", r"setting", r"config")
# The provider, not the topic named after it (provider-events).
PROVIDER = Element("where: the payment provider", (r"provider(?![-_ ]events)", r"upstream", r"third[ -]party"))
NO_ANSWER = Element(
    "what: it hangs or does not answer",
    (
        r"tim(e|es|ed|ing)[ -]?outs?",  # a timeout, times out, timed out, timing out
        r"\bhang",
        r"slow",
        r"unavailable",
        r"\b503\b",
        r"outage",
        r"no answer",
        r"not answer",
        r"unresponsive",
        r"\bdown\b",
    ),
)
RUBRICS_V1 = {
    "duplicate-delivery": Rubric(
        elements=(
            LEDGER,
            Element(
                "what: its dedupe is gone",
                (r"dedup", r"idempoten", r"already[ _-]processed", r"\bnaive\b", r"LEDGER_HANDLER"),
            ),
            Element(
                "how: the provider sends some webhooks again",
                (
                    r"re-?deliver",
                    r"re-?sen[dt]",
                    r"sen[dt]s? (it |them |the same \w+ )?again",
                    r"retried (webhook|deliver|event)",
                    r"(webhook|deliver\w*|event)s? (are |is |get |gets )?retried",
                    r"acknowledg",
                    r"\back\b",
                    r"duplicate (webhook|deliver|event|message)",
                    r"at[ -]least[ -]once",
                ),
            ),
            Element("the deploy that brought it", (*DEPLOY, r"LEDGER_HANDLER", r"handler"), required=False),
        ),
        fix_kinds=("rollback", "config_change"),
        fix_target=r"ledger",
    ),
    "poison-message": Rubric(
        elements=(
            LEDGER,
            Element(
                "what: one message it cannot read",
                (
                    r"poison",
                    r"malformed",
                    r"unreadable",
                    r"(cannot|can't|could not|couldn't|unable to|fails? to|failed to|failing to) (be )?"
                    r"(parse|read|book|process|decode|handle)",
                    r"invalid",
                    r"bad (event|message)",
                    r"decimal",
                    r"12\.50",
                    r"as a string",
                    r"string amount",
                ),
            ),
            Element(
                "how: it retries that message forever, so nothing behind it is booked",
                (r"retr(y|ies|ying|ied)", r"forever", r"endless", r"infinite", r"head[ -]of[ -]line", r"over and over"),
            ),
            Element("the deploy that brought it", (*DEPLOY, r"BAD_EVENTS", r"dead[ -]?letter"), required=False),
        ),
        fix_kinds=("rollback", "config_change"),
        fix_target=r"ledger",
    ),
    "provider-outage": Rubric(
        elements=(
            PROVIDER,
            NO_ANSWER,
            Element(
                "how: our API waits on it, and nothing fails fast",
                (
                    r"circuit[ -]?breaker",
                    r"fail(s|ing)? fast",
                    r"retr(y|ies|ying|ied)",
                    r"attempts",
                    r"threads?\b",
                    r"worker",
                    r"\bwait",
                    r"exhaust",
                    r"saturat",
                ),
            ),
        ),
        fix_kinds=("code_change", "config_change", "none"),
        fix_target=r"payments|provider",
    ),
}
RUBRICS_V2 = RUBRICS_V1 | {
    "provider-outage": Rubric(
        elements=(
            PROVIDER,
            NO_ANSWER,
            Element(
                "how: our API fails fast, its circuit breaker open",
                (
                    r"circuit[ -]?breaker",
                    r"\bbreaker\b",
                    r"fail(s|ed|ing)?[ -]fast",
                    r"short[ -]?circuit",
                    r"circuit (is |was )?(now )?open",
                ),
            ),
        ),
        fix_kinds=("none", "other"),  # nothing to change on our side; escalate to the provider
        fix_target=r"provider|payments",
    ),
}
VERSIONS = {1: RUBRICS_V1, 2: RUBRICS_V2}
RUBRICS = VERSIONS[RUBRIC_VERSION]


def fingerprint(version: int = RUBRIC_VERSION) -> str:
    """The SHA-256 of one version of the rubric: every element, pattern, and fitting fix, for every fault."""
    rubric = {name: asdict(rubric) for name, rubric in sorted(VERSIONS[version].items())}
    return hashlib.sha256(json.dumps(rubric, sort_keys=True).encode()).hexdigest()


def version_of(saved: object) -> int:
    """The rubric version a saved comparison was made with, so a run can be graded again the same way. One saved
    before comparisons recorded their version (the week 3 fault runs, 2026-10-19 UTC) was made with the rubric that
    became version 1: its patterns did not change, and their replay tests grade them again with it. A run that saved
    no comparison gets the latest. Anything else is refused: ValueError."""
    if saved is None:
        return RUBRIC_VERSION
    if not isinstance(saved, Mapping):
        raise ValueError("the saved comparison is not an object")
    if "rubric" not in saved:
        return 1
    rubric = saved["rubric"]
    version = rubric.get("version") if isinstance(rubric, Mapping) else None
    if type(version) is not int or version not in VERSIONS:  # not a bool: True would read as version 1
        raise ValueError("the saved comparison names a rubric version this code does not have")
    return version


def names(element: Element, text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in element.patterns)


def compare(fault_name: str, diagnosis: Mapping | None, version: int = RUBRIC_VERSION) -> dict:
    """The rule's comparison of a diagnosis (as the trace's stop record keeps it) with the cause the fault records,
    both as one version of the rubric has them: the latest, unless told."""
    fault, rubric = faults.get(fault_name), VERSIONS[version][fault_name]
    recorded = fault.recorded(version)
    out: dict = {
        "by": BY[version],
        "rubric": {"version": version, "sha256": fingerprint(version)},
        "fault": fault_name,
        "recorded_cause": recorded.cause,
        "remedy": recorded.remedy,
    }
    if diagnosis is None:
        return out | {"verdict": "no diagnosis"}
    hypothesis = str(diagnosis["hypothesis"])
    found = [(element, names(element, hypothesis)) for element in rubric.elements]
    required = [named for element, named in found if element.required]
    hits = sum(required)
    verdict = (
        "names the cause" if hits == len(required) else "partly" if 2 * hits >= len(required) else "misses the cause"
    )
    fix = diagnosis["proposed_fix"]
    fits = (
        fix["kind"] in rubric.fix_kinds and re.search(rubric.fix_target, str(fix["target"]), re.IGNORECASE) is not None
    )
    return out | {
        "verdict": verdict,
        "hypothesis": hypothesis,
        "elements": [{"element": e.name, "required": e.required, "named": named} for e, named in found],
        "proposed_fix": {"kind": fix["kind"], "target": fix["target"]},
        "fix_fits_the_remedy": fits,
        "fitting_fixes": {"kinds": list(rubric.fix_kinds), "target_matches": rubric.fix_target},
    }


def render(comparison: Mapping) -> list[str]:
    """The comparison in a few lines, with who made it."""
    lines = [f"Compared by {comparison['by']}", f"  recorded cause: {comparison['recorded_cause']}"]
    if comparison["verdict"] == "no diagnosis":
        return [*lines, "  verdict: no diagnosis to compare"]
    lines.append(f"  hypothesis: {clean_text(comparison['hypothesis'])}")  # the model's words: cleaned to print
    for item in comparison["elements"]:
        kind = "required" if item["required"] else "optional"
        lines.append(f"  {'named   ' if item['named'] else 'not named'} ({kind}) {item['element']}")
    fix = comparison["proposed_fix"]
    fits = comparison["fix_fits_the_remedy"]
    lines.append(f"  proposed fix: {fix['kind']} on {clean_text(str(fix['target']), 200)}; fits the remedy: {fits}")
    lines.append(f"  verdict: {comparison['verdict'].upper()}")
    return lines
