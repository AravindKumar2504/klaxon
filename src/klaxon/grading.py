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

Version 3 (week 4, item 4), committed in 75fedf1 before any live run on faults 4 to 6: version 2, and a rubric for
each of them. Versions 1 and 2 have none for them: they came before those faults.

Version 4 (week 5, item 2), committed before any week 5 live run: version 3, a rubric for each of the evaluation's two
new cases (klaxon.evaluation), and the nine known limits fixed where a rule can. Two things are new in it:
- An element can name words that cancel it (unless): words that put that part of the cause somewhere else, or deny
  it. "A deploy of the payment confirmer has a unit bug ... the payments API's records" mentions the payments API, but
  puts the bug in the confirmer; "payments-api has no circuit breaker" names the breaker, and denies it.
- A rubric can name known wrong causes (wrong): a hypothesis that gives one as the cause misses the cause, whatever
  else it names. "Redis lost its idempotency keys" uses fault 1's words, and blames Redis.
The false alarm's rubric asks that the alert be called false, from its lag read near 0, and only a fix of kind none
fits; blaming a real fault (stuck, hung, stalled) misses. Missing-deploys' asks for fault 1's three elements and a
fourth: it says what it could not check. The fourth is required to name the cause (also), and never lifts a wrong cause
to partly. Whether a deploy it claims had evidence, a rule cannot see: that is for the judge (klaxon.judge) and for
Aravind (Aravind's choice, 2026-11-04). Its new patterns were written knowing the
sentences they fix, so those sentences show only that each mechanism works, not that the rule now reads meaning: the
judge and Aravind's own verdicts are the check (klaxon.calibration).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from klaxon import faults
from klaxon.agent.tools import clean_text
from klaxon.agent.trace import read_trace

RUBRIC_VERSION = 4  # the version new comparisons use; each comparison records its own
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
    3: (f"a rule (klaxon.grading, rubric version 3, committed in 75fedf1 before any live run on faults 4 to 6): {HOW}"),
    4: (
        "a rule (klaxon.grading, rubric version 4, committed before any week 5 live run): keyword patterns per element"
        " of the right answer (a fault's recorded cause, or an evaluation case's). Not a person and not a model: it"
        " reads words, not meaning."
    ),
}


@dataclass(frozen=True)
class Element:
    name: str  # what it stands for, in a few words
    patterns: tuple[str, ...]  # regular expressions; one match is enough
    required: bool = True
    unless: tuple[str, ...] = ()  # version 4: one match, and the element is not named (it is put elsewhere, or denied)
    scoped: bool = False  # version 4: a match a negation denies or rules out does not count (_negated)


@dataclass(frozen=True)
class Rubric:
    elements: tuple[Element, ...]
    fix_kinds: tuple[str, ...]  # the kinds of proposed fix that fit the fault's remedy
    fix_target: str  # a regular expression the fix's target must match
    wrong: tuple[Element, ...] = ()  # version 4: known wrong causes; a hypothesis that names one misses the cause
    # Version 4: required too, but only to name the cause: without one, "names the cause" is "partly", and none of them
    # ever lifts a wrong cause to "partly" (missing-deploys: saying what it could not check).
    also: tuple[Element, ...] = ()


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
# The payments API: by name, or as "our API" or "the API".
API = r"payments[-_ ]?api|\b(our|the) api\b"
# Fault 5: a change of the secret, and a gap of a few words within one clause (not across a full stop or semicolon).
SECRET = r"(secret|signing key|hmac|rotat)"
CHANGE = r"(deploy|redeploy|roll(ed|s)?[ -]?out|config)"
GAP = r"[^\w.;]+(\w+[^\w.;]+){0,8}"
# Fault 6: a pool that is not the database's (Redis's, an HTTP client's, a thread or worker pool).
NOT_OTHER = r"(?<!redis )(?<!http )(?<!https )(?<!httpx )(?<!thread )(?<!worker )"
LATE = r"(?! (in time|quickly|fast|soon|promptly|on time))"  # a connection released late is not a leak
RUBRICS_V3 = RUBRICS_V2 | {
    "money-unit": Rubric(
        elements=(
            Element("where: the payments API", (API,)),
            Element(
                "what: our amount in the wrong unit, 100 times over",
                (
                    r"\b100 ?(x|×|times)",
                    r"(x|×|\*) ?100\b",
                    r"hundred(fold| times)",
                    r"(money|amount|currency|charge)[-_ ]?units?\b",
                    r"wrong units?\b|\bunits? (bug|error|mix|mismatch|confusion)",
                    r"CHARGE_AMOUNT_UNIT",
                    r"multipl\w* (it |them |each amount |the amount |amounts )?by 100",
                    r"cents?\W+(\w+\W+){0,6}dollars?|dollars?\W+(\w+\W+){0,6}cents?",
                ),
            ),
            Element(
                "how: the provider charges customers more than our records say",
                (
                    r"overcharg",
                    r"charg\w*\W+(\w+\W+){0,8}(100|hundred|more than|too much)",  # the charge, in any word order
                    r"\b(bill|billed|bills|billing)\W+(\w+\W+){0,3}(100|hundred|more than|too much)",
                    r"provider('s)? charge\w* .{0,40}(more|100|hundred|different)",
                    r"charg\w* \$[\d,]+(\.\d+)? (for|on) (a |an )?\$",  # charged $4,500.00 for a $45.00 payment
                    r"(sends?|sent|sending) [\w.]*\s?(x|×|\*) ?100\b",  # sends amount_cents*100
                    r"amounts? (differ|disagree|do not match|does not match|don't match|doesn't match|mismatch)",
                    r"(differ|disagree|mismatch)\w* .{0,20}amounts?",
                ),
            ),
            Element("the deploy that brought it", (*DEPLOY, r"CHARGE_AMOUNT_UNIT"), required=False),
        ),
        fix_kinds=("rollback", "config_change"),
        fix_target=r"payments",
    ),
    "secret-rotation": Rubric(
        elements=(
            Element("where: the webhook receiver", (r"receiver", r"webhook (endpoint|handler|service)")),
            Element(
                "what: our side moved it to a secret the provider does not sign with",
                (
                    r"WEBHOOK_SECRET_VERSION",
                    r"\bv2\b\W+(\w+\W+){0,3}(secret|key|version)|(secret|key|version)\W+(\w+\W+){0,3}v2\b",
                    rf"(?<!no )(?<!not a )(?<!no recent ){CHANGE}\w*{GAP}{SECRET}",
                    rf"{SECRET}\w*{GAP}(?<!no )(?<!not )(?<!no recent ){CHANGE}",
                    r"provider (still |is still |keeps )?sign\w* (\w+ ){0,3}(old|previous|former|v1|first)",
                    r"(still |only )?(signs?|signed|signing) with (the )?(old|previous|former|v1)",
                    rf"on our side( only)?\W+(\w+\W+){{0,6}}{SECRET}|{SECRET}\w*\W+(\w+\W+){{0,4}}on our side",
                ),
            ),
            Element(
                "how: it refuses every webhook, so nothing reaches the books",
                (
                    r"refus",
                    r"reject",
                    r"(http|status|code|with a|answers?|answered|returns?|returned|responds? with|responded with) 400\b",
                    r"\b400 (bad|error|response|status)",
                    r"bad[_ ]signature",
                    r"signatures? (check |verification )?(fail|mismatch|invalid|do not match|don't match|does not match"
                    r"|doesn't match)",
                    r"(invalid|wrong|mismatched) signature",
                    r"(fail|fails|failing|failed) (its |the |their )?signature",
                ),
            ),
            Element("the deploy that brought it", (*DEPLOY, r"WEBHOOK_SECRET_VERSION"), required=False),
        ),
        fix_kinds=("rollback", "config_change"),
        fix_target=r"receiver",
    ),
    "pool-exhaustion": Rubric(
        elements=(
            Element(
                "where: the payments API's database pool",
                (
                    r"(database|db|psycopg|postgres\w*|sql)[-_ ]?(connection[-_ ]?)?pool",
                    rf"{NOT_OTHER}connection[-_ ]?pool",
                    # "database connections ... its pool"
                    rf"{NOT_OTHER}(database|db|postgres\w*|connection)s?\W+((?!redis|thread|worker|http)\w+\W+){{0,6}}"
                    r"pool\b",
                    r"pool of (\d+ )?(database |db |postgres\w* )?connections",
                    r"out of (database |db |postgres\w* )?connections",
                    r"DB_POOL",
                ),
            ),
            Element(
                "what: its connections are never given back",
                (
                    r"leak",
                    rf"never (\w+ ){{0,2}}(back|returned|released|freed){LATE}",
                    r"never (returns?|releases?|frees?|gives? back|puts? back) (\w+ ){0,2}connections?",
                    rf"connections? (are |is |were |was )?(never|not) (\w+ )?(returned|released|freed|given back|put back)"
                    rf"{LATE}",
                    rf"(not|n't) (\w+ ){{0,2}}(back|returned|released|freed){LATE}",
                    rf"(not|n't) (return|release|free|give back|put back)s?\b{LATE}",
                    r"REFUND_CONNECTION",
                    r"(keeps?|kept) (on to |onto )?(\w+ ){0,2}connections?\b(?! (busy|open|occupied|in use|longer|alive))",
                ),
            ),
            Element(
                "how: with none left, requests wait for a connection and fail",
                (
                    r"PoolTimeout",
                    r"exhaust",
                    r"(run|runs|ran|running) (dry|out)",
                    r"(no|none) (free |available |idle )?connections?",
                    r"(couldn't|could not|cannot|can't) get a connection",
                    r"wait\w* (\w+ ){0,3}for (a |free |an available )?connection",
                ),
            ),
            Element("the deploy that brought it", (*DEPLOY, r"REFUND_CONNECTION"), required=False),
        ),
        fix_kinds=("rollback", "config_change"),
        fix_target=r"payments",
    ),
}


def _gap(words: int) -> str:
    """A gap of a few words within one clause (never across a full stop or a semicolon), none of them a negation:
    "PostgreSQL was down" counts, "PostgreSQL was not down", "PostgreSQL wasn't down" and "neither slow nor down" do
    not."""
    return rf"[^\w.;]+((?!(not|never|no|t|neither|nor)\b)\w+[^\w.;]+){{0,{words}}}"


def _more(element: Element, *patterns: str, unless: tuple[str, ...] = ()) -> Element:
    """Version 3's element, with more patterns, or words that cancel it."""
    return replace(element, patterns=(*element.patterns, *patterns), unless=unless)


V3 = RUBRICS_V3
# Known limit (version 1): "turned off the ledger consumer's duplicate check" is its dedupe gone.
DEDUPE_GONE = _more(
    V3["duplicate-delivery"].elements[1], r"duplicate[- ]?(check|detection|filter\w*|guard|suppression)"
)
REDIS_LOST = Element(  # known limit (version 1): Redis losing its keys is another cause than the handler's deploy
    "Redis lost the keys",
    (
        rf"redis{_gap(3)}(lost|loses|lose|losing|dropped|drops|evicted|evicts|flushed|flushes|expired|wiped|forgot)\b",
        r"\b(lost|dropped|evicted|flushed|expired|wiped) (\w+ ){0,3}(from|in) redis",
    ),
    scoped=True,
)
DATABASE = r"(postgres\w*|database|\bdb\b)"
# Another of our services, where a hypothesis may put a bug (fault 4's known limit).
ELSEWHERE = r"(payment[-_ ]?confirmer|ledger[-_ ]?consumer|webhook[-_ ]?receiver)"
RUBRICS_V4 = RUBRICS_V3 | {
    "duplicate-delivery": replace(
        V3["duplicate-delivery"],
        elements=(V3["duplicate-delivery"].elements[0], DEDUPE_GONE, *V3["duplicate-delivery"].elements[2:]),
        wrong=(REDIS_LOST,),
    ),
    "poison-message": replace(
        V3["poison-message"],
        elements=(
            V3["poison-message"].elements[0],
            # Known limits (version 1): the services' own words for its amount, and "a string" put another way.
            _more(
                V3["poison-message"].elements[1],
                r"not (a |an )?(positive )?(integer|whole number)",
                r"\b(is|was|are|were|as) (a |an )?(decimal |numeric |text |json )?string\b",
            ),
            *V3["poison-message"].elements[2:],
        ),
        # Known limit (version 1): a database outage that makes the ledger retry is another cause.
        wrong=(
            Element(
                "the database was down",
                (rf"{DATABASE}{_gap(2)}(down|unavailable|unreachable|offline|restarting|crashed)\b",),
                scoped=True,
            ),
        ),
    ),
    "provider-outage": replace(
        V3["provider-outage"],
        elements=(
            *V3["provider-outage"].elements[:2],
            # Known limit (version 2): in a system with the breaker, saying there is none names the word, and is wrong;
            # so is saying it never opened. "Without the breaker, each payment would wait" is not a denial.
            _more(
                V3["provider-outage"].elements[2],
                unless=(
                    r"\b(lacks?|lacking|missing|has no|had no|have no|there is no|there's no)\s+(a\s+|an\s+|any\s+|its\s+"
                    r"|the\s+)?(working\s+)?(circuit[ -]?)?breaker",
                    # "Without the breaker, each payment would wait" supposes one away: no denial.
                    r"\b(without|with no|no)\s+(a\s+|an\s+|any\s+|its\s+|the\s+)?(working\s+)?(circuit[ -]?)?breaker\b"
                    r"(?![^.;]*\b(would|could|might)\b)",
                    rf"breaker{_gap(2)}(missing|absent|disabled|removed|turned off|switched off|(is|was|set to|left) off)\b",
                    rf"breaker{_gap(2)}(did not|didn't|never|failed to|does not|doesn't|could not|couldn't|has not|hasn't|had"
                    r" not|hadn't|is not|isn't|was not|wasn't) (yet )?(open|opened|trip|tripped)\b",
                    rf"breaker{_gap(2)}(is|was|stays|stayed|remains|remained) (still )?closed\b",
                    r"PROVIDER_BREAKER\W{0,3}off\b",
                ),
            ),
        ),
        # Known limit (version 1): a slow database is another cause; fake-provider's name holds "provider".
        wrong=(
            Element(
                "the database is slow or down",
                (
                    rf"{DATABASE}{_gap(2)}(slow|down|overloaded|locked|unavailable|saturated)\b",
                    rf"\bslow {DATABASE}",
                ),
                scoped=True,
            ),
        ),
    ),
    "money-unit": replace(
        V3["money-unit"],
        elements=(
            # Known limit (version 3): "the payments API's records", in a hypothesis that puts the bug elsewhere.
            _more(
                V3["money-unit"].elements[0],
                unless=(
                    rf"(deploy\w*|redeploy\w*|bug|change|regression)\W+(of|in|to|on)\W+(the\W+)?{ELSEWHERE}",
                    rf"{ELSEWHERE}{_gap(3)}(has|had|introduced|got|carries|carried)\W+(a\W+|an\W+|the\W+)?"
                    r"((?!(no|not|never|zero)\b)\w+\W+)?(bug|regression)",
                ),
            ),
            # Found by the independent review: "reads amounts as dollars" is the mechanism, with no "cents" said.
            _more(
                V3["money-unit"].elements[1],
                r"(reads?|reading|treats?|treating|takes?|sends?|sending|charges?)\W+(\w+\W+){0,3}(amounts?|cents)\W+"
                r"(\w+\W+){0,2}(as|in) dollars",
            ),
            *V3["money-unit"].elements[2:],
        ),
    ),
    # The evaluation's two new cases (klaxon.evaluation.CASES has their right answers). Their own elements are scoped:
    # a denial does not count ("this is not a false alarm", "it cannot keep up").
    "false-alarm": Rubric(
        elements=(
            Element("where: the ledger consumer group the alert names", (r"\bledger\b",)),
            Element(
                "what: the alert is false, and nothing is wrong",
                (
                    r"\bfalse (alarm|alert|positive)",
                    rf"\b(alert|alarm){_gap(4)}(is|was|looks|seems|appears){_gap(2)}"
                    r"(wrong|false|mistaken|incorrect|spurious|stale|bogus|unfounded|inaccurate|misleading|untrue)\b",
                    r"\bnothing (is |was |seems |looks |appears )?(to be )?(wrong|broken|amiss|behind)\b",
                    r"\bspurious\b",
                    r"\bnot (a |an )?(real|genuine|actual|true) (alert|alarm|incident|problem|issue|fault|backlog)",
                ),
                scoped=True,
            ),
            Element(
                "how it knows: the lag reads near 0, and the group keeps up",
                (
                    # A lag near 0, but not one that rose from 0 to more.
                    rf"\blag\b{_gap(4)}(?<!from )(\b0\b|zero|near (0|zero)|normal|low|small|negligible|nil|none|empty)"
                    r"(?!\s+(to|and|then)\s+\d)",
                    r"\b(0|zero) (messages? )?(behind|lag)\b",
                    r"\b(caught|catching|catches|keeps?|keeping|kept) up\b",
                    r"\bup to date\b",
                    r"\bno (consumer |real )?(lag|backlog)\b",
                    r"\bnot (actually |really |at all )?(behind|lagging)\b",
                    r"\b(reads?|is|shows?|stays?|sits?)\s+(at\s+)?(0|zero)\b(?!\s+(to|and|then)\s+\d)",  # "it reads 0"
                ),
                scoped=True,
            ),
        ),
        fix_kinds=("none",),  # nothing to fix
        fix_target=r"",  # on any target
        wrong=(
            Element(
                "a real fault in the ledger consumer (stuck, hung, stalled, crashed)",
                (
                    r"\b(stuck|hung|hangs|hanging|stall|stalls|stalled|stalling|frozen|freezes|deadlock\w*|crash|crashed"
                    r"|crashes|crashing|wedged|poison\w*)\b",
                ),
                scoped=True,
            ),
        ),
    ),
    "missing-deploys": Rubric(
        elements=(
            V3["duplicate-delivery"].elements[0],
            DEDUPE_GONE,
            V3["duplicate-delivery"].elements[2],
            Element(
                "a deploy it names (the tools that show deploys did not answer: its evidence is for a person to check)",
                (*DEPLOY, r"LEDGER_HANDLER", r"handler"),
                required=False,
            ),
        ),
        fix_kinds=V3["duplicate-delivery"].fix_kinds,
        fix_target=V3["duplicate-delivery"].fix_target,
        wrong=(REDIS_LOST,),
        also=(
            Element(
                "what it could not check: docker did not answer the deploy list or the health check",
                (
                    # Klaxon could not check something docker shows: not "the handler did not check for duplicates".
                    r"\b(could not|couldn't|cannot|can't|unable to|did not|didn't|failed to)\s+(\w+\s+){0,2}"
                    r"(check|read|see|confirm|verify|list|reach|query|inspect|retrieve|access|get)\b[^.;]{0,60}?"
                    r"\b(deploy\w*|docker|container\w*|health\w*|tools?|recent_deploys|service_health|setting\w*|history)\b",
                    r"\b(deploy\w*|docker|container\w*|health\w*|recent_deploys|service_health)\b[^.;]{0,60}?"
                    r"\b(could not|couldn't|cannot|can't) be (read|checked|seen|confirmed|verified|listed|reached)\b",
                    r"\bdocker[^\w.;]+(\w+[^\w.;]+){0,4}(did not answer|didn't answer|not answer|no answer|timed out"
                    r"|times out|time ?out|unavailable|unreachable|not responding|unresponsive|down\b|did not respond"
                    r"|didn't respond|never responded|never answered|no response)",
                    r"\b(recent_deploys|service_health|deploy (list|log|history)|deploy tool|health (check|tool))"
                    r"[^\w.;]+(\w+[^\w.;]+){0,4}(failed|fails|timed out|times out|errored|error|unavailable|did not"
                    r" answer|didn't answer|not available|returned nothing|did not respond|didn't respond|never answered)",
                    r"\b(deploy\w*|docker|container\w*|health\w*|setting\w*|handler|recent_deploys|service_health)\b"
                    r"[^.;]{0,80}?\b(unverified|unconfirmed|not (been |yet )?(confirmed|verified|checked))\b",
                    r"\b(unverified|unconfirmed|not (been |yet )?(confirmed|verified|checked))\b[^.;]{0,40}?"
                    r"\b(deploy\w*|docker|container\w*|health\w*|setting\w*|handler)\b",
                ),
                # Not scoped: its own words are negations ("did not answer"), and the outage's negations come with them.
            ),
        ),
    ),
}
VERSIONS = {1: RUBRICS_V1, 2: RUBRICS_V2, 3: RUBRICS_V3, 4: RUBRICS_V4}
RUBRICS = VERSIONS[RUBRIC_VERSION]


def _shape(rubric: Rubric) -> dict:
    """A rubric as data, for its fingerprint. Version 4's new fields are left out where they are empty, so each earlier
    version keeps the fingerprint pinned before its runs."""
    shape = asdict(rubric)
    for field in ("wrong", "also"):
        if not shape[field]:
            del shape[field]
    for element in [*shape["elements"], *shape.get("wrong", []), *shape.get("also", [])]:
        if not element["unless"]:
            del element["unless"]
        if not element["scoped"]:
            del element["scoped"]
    return shape


def fingerprint(version: int = RUBRIC_VERSION) -> str:
    """The SHA-256 of one version of the rubric: every element, pattern, and fitting fix, for every fault."""
    rubric = {name: _shape(rubric) for name, rubric in sorted(VERSIONS[version].items())}
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


# Version 4: what denies a match, or offers it only as a maybe, read within its own part of a sentence. A clause ends at a
# full stop, a semicolon, a colon or an opening bracket. Inside it, a comma ends a segment, except through a list of the
# same kind of thing ("not hung, stalled or crashed"): a negation reaches no further (the re-check's finding H1: "The
# provider is not down, the database is overloaded" blames the database). In a segment, a turn of the sentence ("but",
# "and", "while" ...) after a negation ends its reach.
CUE = re.compile(
    r"\b(not|no|never|neither|nor|without|cannot|nothing|none|unlikely|excluded|rather than|instead of"
    r"|rule[sd]? out|ruling out|if)\b|n't\b",
    re.IGNORECASE,
)
TURN = re.compile(
    r"\b(but|however|although|though|whereas|while|when|after|until|as|yet|so|because|since|instead|and|then)\b",
    re.IGNORECASE,
)
# After a match, in its segment: ruled out ("is unlikely", "and found none"), or only supposed ("would show").
RULED_OUT = re.compile(
    r"\b(ruled out|excluded|unlikely|not the cause|not at fault|not involved|not to blame|found none|found nothing"
    r"|found no sign|would)\b",
    re.IGNORECASE,
)
OBJECT_DENIED = re.compile(r"\s+(nothing|none|no)\b", re.IGNORECASE)  # right after a match: "Redis lost nothing"
RULING_OUT_LEAD = re.compile(r"\s*(ruled out|excluded|unlikely|not likely|not the cause)\s*", re.IGNORECASE)
CLAUSE = ".;!?:("
SEGMENT_ENDS = ".;!?:(),"
# A wrong cause offered as a maybe, beside the right one: "the database could be down too", "Still open: whether...",
# ", or PostgreSQL was briefly unavailable". "Could not" is no hedge; "open" is one only before a colon ("Still open:").
HEDGE = re.compile(
    r"\b(could(?!\s*(not|n't))|might|may|possibly|perhaps|maybe|whether|either|unclear|uncertain)\b", re.IGNORECASE
)
HEDGE_LEAD = re.compile(r"\b(open|unknown|unclear|possible|possibly|maybe|perhaps|unverified)\b", re.IGNORECASE)
MAYBE_AFTER = re.compile(
    r"\b(is possible|was possible|possibly|a possibility|cannot be ruled out|can't be ruled out|not ruled out)\b",
    re.IGNORECASE,
)


def _clause(text: str, start: int) -> tuple[int, str]:
    """Where the clause of a match at start begins (after a full stop, a semicolon, a colon or an opening bracket), and
    the words before its colon when a colon opens it ("Ruled out: ...", "Still open: ...")."""
    opened = max(text.rfind(mark, 0, start) for mark in CLAUSE) + 1
    if opened and text[opened - 1] == ":":
        return opened, text[max(text.rfind(mark, 0, opened - 1) for mark in CLAUSE) + 1 : opened - 1]
    return opened, ""


def _segment(text: str, start: int, opened: int, patterns: tuple[str, ...]) -> int:
    """Where the comma segment of a match at start begins: after the last comma of its clause; or, when the segments
    before it each end in another match of the same patterns, where that list begins ("not hung, stalled or crashed")."""
    begins = max(text.rfind(",", opened, start) + 1, opened)
    while begins > opened:
        before = max(text.rfind(",", opened, begins - 1) + 1, opened)
        piece = text[before : begins - 1]
        if not any(re.search(rf"(?:{pattern})\W*$", piece, re.IGNORECASE) for pattern in patterns):
            break
        begins = before
    return begins


def _segment_end(text: str, end: int) -> int:
    found = [i for i in (text.find(mark, end) for mark in SEGMENT_ENDS) if i != -1]
    return min(found, default=len(text))


def _negated(text: str, start: int, end: int, patterns: tuple[str, ...] = ()) -> bool:
    """Whether the match at text[start:end] (of one of patterns) is denied, ruled out or only supposed (version 4): a
    negation earlier in its segment, with no turn of the sentence after it ("no sign of a poison message", "rather
    than Redis losing keys", "if it were stuck"), through a list of the same kind ("not hung, stalled or crashed"); a
    ruling-out before a colon ("Ruled out: Redis losing its keys"); or, later in its segment, words that rule it out
    or only suppose it ("is unlikely", "and found none", "would show"), or a denied object right after it ("Redis
    lost nothing")."""
    opened, lead = _clause(text, start)
    if RULING_OUT_LEAD.fullmatch(lead):
        return True
    begins = _segment(text, start, opened, patterns)
    cues = list(CUE.finditer(text, begins, start))
    if cues and not TURN.search(text, cues[-1].end(), start):
        return True
    if OBJECT_DENIED.match(text, end):
        return True
    return RULED_OUT.search(text, end, _segment_end(text, end)) is not None


def _hedged(text: str, start: int, end: int, patterns: tuple[str, ...] = ()) -> bool:
    """Whether the match at text[start:end] is offered as a maybe (version 4): a hedge in it ("could be down"); earlier
    in its segment with no turn of the sentence after it ("might", "whether"), or as an alternative (", or PostgreSQL
    was down"); one before its colon ("Still open: ..."); or later in its segment ("is possible", "cannot be ruled
    out")."""
    opened, lead = _clause(text, start)
    if HEDGE_LEAD.search(lead) or HEDGE.search(lead) or HEDGE.search(text, start, end):
        return True
    begins = _segment(text, start, opened, patterns)
    if text[begins:start].strip().lower() in ("or", "either", "or else"):  # an alternative: ", or X"
        return True
    hedges = list(HEDGE.finditer(text, begins, start))
    if hedges and not TURN.search(text, hedges[-1].end(), start):
        return True
    return MAYBE_AFTER.search(text, end, _segment_end(text, end)) is not None


def _stance(element: Element, text: str) -> str | None:
    """How text gives a known wrong cause (version 4): "asserted", "offered" (only as a maybe), or None (not at all, or
    only denied)."""
    offered = False
    for pattern in element.patterns:
        for found in re.finditer(pattern, text, re.IGNORECASE):
            if _negated(text, found.start(), found.end(), element.patterns):
                continue
            if not _hedged(text, found.start(), found.end(), element.patterns):
                return "asserted"
            offered = True
    return "offered" if offered else None


def _found(patterns: tuple[str, ...], text: str, scoped: bool = False) -> bool:
    if not scoped:
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    return any(  # version 4: only a match no negation denies
        not _negated(text, found.start(), found.end(), patterns)
        for pattern in patterns
        for found in re.finditer(pattern, text, re.IGNORECASE)
    )


def names(element: Element, text: str) -> bool:
    """Whether text names the element: one of its patterns (version 4: for a scoped element, one no negation denies),
    and none of the words that cancel it (version 4: words it asserts, never ones it denies)."""
    return _found(element.patterns, text, element.scoped) and not _found(element.unless, text, scoped=True)


def answer(name: str, version: int = RUBRIC_VERSION) -> faults.Recorded:
    """The right answer a comparison by this version reads: a fault's recorded cause and remedy, as that version has
    them, or an evaluation case's right answer and remedy (klaxon.evaluation.CASES, committed before any run on it)."""
    if name in faults.FAULTS:
        return faults.FAULTS[name].recorded(version)
    from klaxon.evaluation import CASES  # here, not at the top: klaxon.evaluation imports this module

    if name not in CASES:
        raise ValueError(f"no fault or evaluation case {name!r}")
    return faults.Recorded(CASES[name].right_answer, CASES[name].remedy)


def newest_for(name: str, version: int) -> int:
    """The newest rubric version that grades name against the same right answer as version: the one to grade a saved
    run again with, on the system it ran on. Week 3's run of fault 3 ran before the circuit breaker, which versions 2
    to 4 assume: it stays with version 1."""
    if name not in VERSIONS.get(version, {}):
        raise ValueError(f"rubric version {version} has no rubric for {name}")
    same = answer(name, version)
    return max(v for v in VERSIONS if v >= version and name in VERSIONS[v] and answer(name, v) == same)


def compare(fault_name: str, diagnosis: Mapping | None, version: int = RUBRIC_VERSION) -> dict:
    """The rule's comparison of a diagnosis (as the trace's stop record keeps it) with the cause the fault records (or
    an evaluation case's right answer), both as one version of the rubric has them: the latest, unless told."""
    if fault_name not in faults.FAULTS and fault_name not in RUBRICS_V4:
        raise ValueError(f"no fault or evaluation case {fault_name!r}")
    if fault_name not in VERSIONS[version]:  # a version that came before the fault: it has no rubric for it
        raise ValueError(f"rubric version {version} has no rubric for {fault_name}")
    rubric = VERSIONS[version][fault_name]
    recorded = answer(fault_name, version)
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
    also = [(element, names(element, hypothesis)) for element in rubric.also]
    if verdict == "names the cause" and not all(named for _, named in also):  # version 4: the cause, and not the rest
        verdict = "partly"
    stances = {element.name: _stance(element, hypothesis) for element in rubric.wrong}
    wrong = [name for name, said in stances.items() if said == "asserted"]
    offered = [name for name, said in stances.items() if said == "offered"]
    if wrong:  # version 4: a hypothesis that gives a known wrong cause as the cause misses it, whatever else it names
        verdict = "misses the cause"
    elif offered and verdict == "names the cause":  # version 4: the right cause beside a wrong one, unchosen
        verdict = "partly"
    fix = diagnosis["proposed_fix"]
    fits = (
        fix["kind"] in rubric.fix_kinds and re.search(rubric.fix_target, str(fix["target"]), re.IGNORECASE) is not None
    )
    elements = []
    for element, named, to_name in [(e, n, False) for e, n in found] + [(e, n, True) for e, n in also]:
        item = {"element": element.name, "required": element.required, "named": named}
        if version >= 4:  # its words were there, and cancelled: put elsewhere, or denied
            item["cancelled"] = _found(element.patterns, hypothesis) and not named
        if to_name:  # required only to name the cause (Rubric.also)
            item["only_to_name_the_cause"] = True
        elements.append(item)
    out |= {
        "verdict": verdict,
        "hypothesis": hypothesis,
        "elements": elements,
    }
    if version >= 4:
        out |= {"wrong_causes": wrong, "wrong_causes_offered": offered}
    return out | {
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
        kind = "required to name the cause" if item.get("only_to_name_the_cause") else kind
        said = "named   " if item["named"] else "not named"
        cancelled = " (its words are there, but put elsewhere or denied)" if item.get("cancelled") else ""
        lines.append(f"  {said} ({kind}) {item['element']}{cancelled}")
    for name in comparison.get("wrong_causes", []):
        lines.append(f"  names a known wrong cause: {name}")
    for name in comparison.get("wrong_causes_offered", []):
        lines.append(f"  offers a known wrong cause as a maybe: {name}")
    fix = comparison["proposed_fix"]
    fits = comparison["fix_fits_the_remedy"]
    lines.append(f"  proposed fix: {fix['kind']} on {clean_text(str(fix['target']), 200)}; fits the remedy: {fits}")
    lines.append(f"  verdict: {comparison['verdict'].upper()}")
    return lines


# A saved run, as a grader reads it.

RUN_FILE, TRACE = "run.json", "trace.jsonl"  # as klaxon.fault_runs and klaxon.agent.loop name them
# Week 3's first live run (make investigate, 2026-10-18) saved no run.json: an alert given by hand ("the ledger
# consumer's lag on provider-events is above 100 messages") on a healthy stack, which is the false alarm's case. It is
# graded against the false alarm's right answer as version 4 has it, the first with one, whatever version comes next. A
# stated limit (the review's finding L6): that stack had no traffic, and the right answer says "under traffic its lag
# stays near 0"; what it asks for (not behind, the alert false) holds there too.
HAND_GIVEN = {"2026-10-18T112951Z_loop_ledgerlaghigh": ("false-alarm", 4)}


@dataclass(frozen=True)
class Saved:
    """A saved run, as a grader reads it: its fault or case; the rubric version whose right answer is the system it
    ran on (the version that graded it, or the latest); and the diagnosis it ended in (None when it gave none)."""

    folder: Path
    name: str
    version: int
    diagnosis: dict | None


def saved(folder: Path | str) -> Saved:
    """Read a saved run's folder: its run.json, whose loop keeps the diagnosis the trace's stop record holds; or, for
    week 3's first live run, what HAND_GIVEN says, and its trace. ValueError, KeyError or OSError when it is not a run a
    grader can read."""
    folder = Path(folder)
    if (folder / RUN_FILE).is_file():
        run = json.loads((folder / RUN_FILE).read_text())
        if not isinstance(run, dict):
            raise ValueError(f"{folder.name}'s {RUN_FILE} is not an object")
        case = run.get("case") or {}
        name = case.get("name") if isinstance(case, Mapping) and case.get("name") else run["fault"]["name"]
        version = version_of(run.get("comparison"))
        found = (run.get("loop") or {}).get("diagnosis")  # None: the loop never ran, did not finish, or gave none
    elif folder.name in HAND_GIVEN:
        name, version = HAND_GIVEN[folder.name]
        stop = read_trace(folder / TRACE)[-1]
        found = stop.get("diagnosis") if stop.get("kind") == "stop" else None
    else:
        raise ValueError(f"{folder.name} holds no {RUN_FILE}: not a run a grader can read")
    if name not in VERSIONS[version]:
        raise ValueError(f"rubric version {version} has no rubric for {name}")
    return Saved(folder, name, version, found if isinstance(found, dict) else None)


def run_folders(paths: Iterable[Path | str]) -> list[Path]:
    """The saved runs in these paths, each once, in order: a run's own folder, or the runs inside a folder of them (a
    week's, a suite's, or a week's suites). Rehearsals (no model) and folders that are not runs are passed over."""
    found: list[Path] = []
    seen: set[Path] = set()

    def visit(path: Path, depth: int) -> None:
        key = path.resolve()
        if key in seen or not path.is_dir():
            return
        seen.add(key)
        if (path / RUN_FILE).is_file() or path.name in HAND_GIVEN:
            if "_rehearsal_" not in path.name:
                found.append(path)
        elif depth < 2:
            for child in sorted(child for child in path.iterdir() if child.is_dir()):
                visit(child, depth + 1)

    for path in paths:
        visit(Path(path), 0)
    return found


def digest(value: object) -> str:
    """The SHA-256 of a value as compact JSON with sorted keys: how a judgment and a person's verdict name the
    diagnosis they graded, so a later change to the evidence shows."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
