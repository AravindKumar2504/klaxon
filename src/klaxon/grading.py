"""How a diagnosis compares with the cause a fault records: a rule, written before Klaxon's first runs on the faults.

klaxon.faults records each fault's cause in plain words. Here each cause is cut into three elements a correct
diagnosis names: where it is (the ledger consumer, say), what is wrong there (its dedupe is gone), and how that
produces the symptom (the provider sends some webhooks again). Words that only restate the alert (lag, a stall,
events posted twice) are not elements: the alert already says them. Each element is a few regular expressions,
matched without regard to case. The rule reads the diagnosis's hypothesis, the field the diagnosis contract keeps for
"the cause, and how it produces the alert", and counts the required elements it names:

    names the cause    all of them
    partly             at least half of them
    misses the cause   fewer than half

Optional elements (the deploy that brought the bad setting) are reported, and do not change the verdict. The rule
also says whether the proposed fix is of a kind, and on a target, that fit the fault's remedy.

A rule reads words, not meaning: "a deploy is unlikely" names a deploy, and the right cause put in other words can be
missed. So the evidence shows its verdict next to the hypothesis and the recorded cause, for a person to check, and
it is always labeled as a rule's. Week 5's evaluation grades many runs and can compare graders.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from klaxon import faults
from klaxon.agent.tools import clean_text

BY = (
    "a rule (klaxon.grading): keyword patterns per element of the fault's recorded cause, written and committed"
    " before the live runs. Not a person and not a model: it reads words, not meaning."
)


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
RUBRICS = {
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
            # The provider, not the topic named after it (provider-events).
            Element("where: the payment provider", (r"provider(?![-_ ]events)", r"upstream", r"third[ -]party")),
            Element(
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
            ),
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


def names(element: Element, text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in element.patterns)


def compare(fault_name: str, diagnosis: Mapping | None) -> dict:
    """The rule's comparison of a diagnosis (as the trace's stop record keeps it) with the fault's recorded cause."""
    fault, rubric = faults.get(fault_name), RUBRICS[fault_name]
    out: dict = {"by": BY, "fault": fault_name, "recorded_cause": fault.cause, "remedy": fault.remedy}
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
