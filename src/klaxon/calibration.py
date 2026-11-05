"""The graders checked (week 5, item 2): the rule and the judge, on sentences with an expected verdict and on the saved
diagnoses, and both against Aravind's own verdicts.

A grader nobody has checked measures nothing. Two sets, Aravind's choice (2026-11-04), 79 items:
- SENTENCES: the 66 hypotheses of the grading tests (tests/test_grading.py reads them from here), each with the verdict
  the tests expect, and the system it was written for (a fault's cause as one rubric version records it: fault 3 had
  no circuit breaker in version 1's). Claude wrote them, with their expected verdicts, in the sessions that built each
  rubric and in their independent reviews (the nine known limits, with the right verdict); Aravind accepted those items,
  but did not write or label these sentences. They are not Klaxon's output. So on them the judge is checked against
  verdicts Claude expected: a check of the judge's reading, not a person's.
- SAVED_RUNS: the 13 saved diagnoses: the 12 fault runs of weeks 3 and 4, and week 3's first live run, on a false
  alarm. Recorded responses. Aravind's verdicts on them come from make label (klaxon.labels), in his review: they are
  the person's check on both graders.

The judge sees each item's right answer and remedy for the system it was written for, and the diagnosis (a sentence is
a hypothesis alone): one recorded call each. A call that gives no reply Klaxon can use stops the calibration, and what
was judged is kept. The rule grades each item with the newest rubric written for that system (grading.newest_for):
version 4, but version 1 for fault 3 before the breaker, which only version 1 has. Version 4's new patterns were written
knowing these sentences (and the false alarm's, knowing week 3's first diagnosis), so on them the rule shows that each
fix works, not that it reads meaning. The judge's prompt was written by the session that knew them too, though it names
none of them.

Until Aravind has labeled a saved run, the report holds back both graders' verdicts on it, so he gives his own first
(the independent review's finding M6).

Run: python -m klaxon.calibration run              live: the judge on every sentence and saved run (his OK first)
     python -m klaxon.calibration report [FOLDER]  offline: each grader against the expected verdicts and his own
     python -m klaxon.calibration replay [FOLDER]  offline: the recorded judgments again
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from klaxon import grading, judge, labels
from klaxon.agent import loop
from klaxon.agent.loop import find_live_model
from klaxon.agent.model import Model, ModelError, ReplayModel
from klaxon.agent.tools import clean_text
from klaxon.patient.stack import REPO

V1_TESTS = "version 1's tests"
V2_TESTS = "version 2's tests"
V3_TESTS = "version 3's tests"
V1_LIMIT = "a known limit of version 1 (week 3, item 4's review)"
V2_LIMIT = "a known limit of version 2 (week 4, item 3)"
V3_LIMIT = "a known limit of version 3 (week 4, item 4's review)"
EVIDENCE = REPO / "evidence" / "week5"
CALIBRATION, RECORDING = "calibration.json", judge.RECORDING
NO_FIX = {"kind": "none", "target": "none"}  # a sentence has no fix: the rule needs one to compare, and it is not read
HELD_BACK = "held back until Aravind labels it"
LABEL = (
    "The judge's calibration on the grading tests' sentences (klaxon.calibration): one recorded call per sentence, each"
    " with the right answer of the system it was written for and the hypothesis alone. The sentences and their expected"
    " verdicts were written by Claude, for the tests (not by Klaxon, and not by Aravind); the judgments are recorded"
    " responses, in judge-recording.jsonl."
)
WHAT = (
    "The judge's calibration: one call per sentence of the grading tests, each with its right answer, on Aravind's"
    " seat. The sentences were written by Claude, for the tests."
)


@dataclass(frozen=True)
class Sentence:
    """A hypothesis written for the grading tests, with the verdict the tests expect."""

    name: str  # the fault whose right answer it is graded against
    system: int  # the rubric version whose right answer it was written for: the system it describes
    hypothesis: str
    expected: str
    source: str  # which tests it came from, or the review that found it a known limit


SENTENCES = (
    Sentence(
        "duplicate-delivery",
        1,
        "A redeploy of the ledger consumer switched it to a handler without dedupe, so every webhook the provider redelivers after a lost ack is booked a second time.",
        "names the cause",
        V1_TESTS,
    ),
    Sentence(
        "duplicate-delivery",
        1,
        "The ledger consumer no longer checks idempotency keys, so some events are posted twice.",
        "partly",
        V1_TESTS,
    ),
    Sentence("duplicate-delivery", 1, "The ledger is posting some events twice.", "misses the cause", V1_TESTS),
    Sentence(
        "poison-message",
        1,
        "One malformed payment.updated event is retried forever by the ledger consumer (BAD_EVENTS=retry after a deploy), so nothing behind it is booked.",
        "names the cause",
        V1_TESTS,
    ),
    Sentence("poison-message", 1, "The ledger consumer keeps retrying and is stuck.", "partly", V1_TESTS),
    Sentence("poison-message", 1, "The ledger consumer has stalled and its lag grows.", "misses the cause", V1_TESTS),
    Sentence(
        "provider-outage",
        1,
        "The payment provider is timing out, and with no circuit breaker each payment waits through three attempts.",
        "names the cause",
        V1_TESTS,
    ),
    Sentence("provider-outage", 1, "The provider is down.", "partly", V1_TESTS),
    Sentence("provider-outage", 1, "The database connection pool is exhausted.", "misses the cause", V1_TESTS),
    Sentence(
        "provider-outage",
        2,
        "The payment provider hangs: our calls to it time out, the circuit breaker opened, and payments-api now fails fast with 503.",
        "names the cause",
        V2_TESTS,
    ),
    Sentence(
        "provider-outage", 2, "The provider is unavailable, and our breaker is open.", "names the cause", V2_TESTS
    ),
    Sentence("provider-outage", 2, "The provider is down.", "partly", V2_TESTS),
    Sentence("provider-outage", 2, "payments-api short-circuits every payment with a 503.", "partly", V2_TESTS),
    Sentence("provider-outage", 2, "The database connection pool is exhausted.", "misses the cause", V2_TESTS),
    Sentence(
        "provider-outage",
        2,
        "The provider times out and payments-api waits through three attempts, its workers tied up.",
        "partly",
        V2_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "A deploy of payments-api reads our amounts, already in cents, as dollars, so the provider charges each customer 100 times the amount.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "Since its deploy, payments-api sends 100x amounts: the provider's charges are 100x ours.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "The payments-api deploy multiplies each amount by 100: each charge sent to the provider is 100x our recorded amount.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "After the payments-api deploy (CHARGE_AMOUNT_UNIT=dollars), customers are charged 100 times the amount.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "payments-api has a money-unit bug since its deploy: the provider bills customers 100x.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "The payments API now treats cents as dollars, so every charge is 100 times the payment amount.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "A deploy of the payments API converts amounts to cents twice; charges at the provider are 100x ours.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "Since its deploy, payments-api sends amount_cents*100 to the provider.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "The payments API reads cents as dollars: every customer is charged a hundredfold.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "money-unit",
        3,
        "The payments API's charge unit changed to dollars: the provider charged $4,500.00 for a $45.00 payment.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence("money-unit", 3, "payments-api overcharges customers.", "partly", V3_TESTS),
    Sentence("money-unit", 3, "Our payments disagree with the provider's charges.", "misses the cause", V3_TESTS),
    Sentence(
        "money-unit",
        3,
        "The payment confirmer converts amounts to dollars and fails a unit test.",
        "misses the cause",
        V3_TESTS,
    ),
    Sentence("money-unit", 3, "The payments API has a unit test failure.", "misses the cause", V3_TESTS),
    Sentence(
        "secret-rotation",
        3,
        "The webhook receiver was deployed with a rotated secret the provider does not sign with, so it refuses every webhook with a 400.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "A deploy rotated our webhook secret on the receiver only; the provider still signs with the old one, so every signature check fails.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "The webhook receiver's signing secret was rotated, but the provider still signs with the old one, so every webhook fails its signature check and is refused.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "The secret was rotated on our side only: the webhook receiver now checks v2, and rejects every webhook the provider signs with v1.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "webhook-receiver was redeployed with WEBHOOK_SECRET_VERSION=v2; every webhook gets a 400 bad_signature.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "A recent config change moved the receiver to a new HMAC secret, so it rejects every webhook as a signature mismatch.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence("secret-rotation", 3, "Webhooks fail their signature check at the receiver.", "partly", V3_TESTS),
    Sentence(
        "secret-rotation",
        3,
        "Our books are behind the provider: its webhooks are not acknowledged.",
        "misses the cause",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "The provider rotated its webhook signing key and our webhook receiver still checks the old one, so it rejects every webhook.",
        "partly",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "The receiver is down; 400 of the provider's webhooks are pending.",
        "misses the cause",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "No recent deploy touched the receiver; the provider must have rotated its signing secret, so every signature check fails.",
        "partly",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "The provider moved to v2 of its webhook format, and our receiver rejects the new payloads.",
        "partly",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "The provider rotated its secret; our receiver's config still has the old key, so it rejects every webhook.",
        "partly",
        V3_TESTS,
    ),
    Sentence(
        "secret-rotation",
        3,
        "The provider rotated its signing key, so our secret is now stale and the receiver rejects every webhook.",
        "partly",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "A payments-api deploy leaks database connections in its refund path: its pool runs dry, and requests wait for a connection until they time out.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "Since its deploy, payments-api leaks database connections: the connection pool is exhausted and every request hits PoolTimeout.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "The payments API's refund path does not return its database connection to the pool, so the pool of 10 runs out and requests fail.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "payments-api never releases the connections it takes for refunds (REFUND_CONNECTION=kept), so its psycopg pool is empty and requests fail with PoolTimeout.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "A deploy of payments-api leaks database connections, until it runs out of connections and requests fail with PoolTimeout.",
        "names the cause",
        V3_TESTS,
    ),
    Sentence("pool-exhaustion", 3, "The payments API's database pool is exhausted.", "partly", V3_TESTS),
    Sentence("pool-exhaustion", 3, "Payments are slow and fail with 500s.", "misses the cause", V3_TESTS),
    Sentence("pool-exhaustion", 3, "The provider is down, and our breaker is open.", "misses the cause", V3_TESTS),
    Sentence(
        "pool-exhaustion",
        3,
        "Slow database queries in payments-api are holding connections, so requests hit PoolTimeout.",
        "misses the cause",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "The provider hangs; our API's worker threads hold their connections while they wait, the thread pool is exhausted, and requests fail.",
        "misses the cause",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "payments-api leaks Redis connections, until the Redis connection pool is exhausted.",
        "partly",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "The provider never returns an answer, so the payments API's HTTP connection pool to it is exhausted and requests fail.",
        "misses the cause",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "Database connections are not released in time, so the connection pool is exhausted and requests fail.",
        "partly",
        V3_TESTS,
    ),
    Sentence(
        "pool-exhaustion",
        3,
        "A long-running transaction keeps database connections busy, exhausting the connection pool.",
        "partly",
        V3_TESTS,
    ),
    Sentence(
        "poison-message",
        1,
        "The ledger consumer could not process events while PostgreSQL was down; it is retrying.",
        "misses the cause",
        V1_LIMIT,
    ),
    Sentence(
        "duplicate-delivery",
        1,
        "Redis lost its idempotency keys, so the ledger books webhooks the provider re-sends a second time.",
        "misses the cause",
        V1_LIMIT,
    ),
    Sentence(
        "provider-outage",
        1,
        "PostgreSQL is slow and payments-api requests wait on it; fake-provider looks healthy.",
        "misses the cause",
        V1_LIMIT,
    ),
    Sentence(
        "provider-outage",
        1,
        "payments-api is slow because a traffic spike exhausted its worker threads.",
        "misses the cause",
        V1_LIMIT,
    ),
    Sentence(
        "poison-message",
        1,
        "The ledger consumer retries forever one payment.updated event whose amount is not positive integer cents.",
        "names the cause",
        V1_LIMIT,
    ),
    Sentence(
        "poison-message",
        1,
        "The ledger consumer retries forever one event whose amount is a string.",
        "names the cause",
        V1_LIMIT,
    ),
    Sentence(
        "duplicate-delivery",
        1,
        "A deploy turned off the ledger consumer's duplicate check, so webhooks the provider sends again are booked twice.",
        "names the cause",
        V1_LIMIT,
    ),
    Sentence(
        "provider-outage",
        2,
        "The provider times out, and payments-api has no circuit breaker, so every payment waits through three tries.",
        "partly",
        V2_LIMIT,
    ),
    Sentence(
        "money-unit",
        3,
        "A deploy of the payment confirmer has a unit bug: it reads the provider's amounts as dollars, so the amounts differ from the payments API's records.",
        "partly",
        V3_LIMIT,
    ),
)
# The 13 saved diagnoses, from the repo's root: week 3's first live run (a false alarm), week 3's three fault runs and
# week 4's nine gated runs. Recorded responses.
SAVED_RUNS = (
    "evidence/week3/2026-10-18T112951Z_loop_ledgerlaghigh",
    "evidence/week3/2026-10-19T022933Z_fault-run_duplicate-delivery",
    "evidence/week3/2026-10-19T023414Z_fault-run_poison-message",
    "evidence/week3/2026-10-19T024001Z_fault-run_provider-outage",
    "evidence/week4/2026-10-21T090513Z_gated-run_duplicate-delivery",
    "evidence/week4/2026-10-21T091242Z_gated-run_poison-message",
    "evidence/week4/2026-10-21T094537Z_gated-run_poison-message",
    "evidence/week4/2026-10-24T081041Z_gated-run_money-unit",
    "evidence/week4/2026-10-24T082544Z_gated-run_money-unit",
    "evidence/week4/2026-10-24T083634Z_gated-run_secret-rotation",
    "evidence/week4/2026-11-01T024929Z_gated-run_secret-rotation",
    "evidence/week4/2026-11-01T030525Z_gated-run_pool-exhaustion",
    "evidence/week4/2026-11-01T031219Z_gated-run_provider-outage",
)


def rule_on(sentence: Sentence) -> dict:
    """The rule's verdict on a sentence: the newest rubric written for its system."""
    version = grading.newest_for(sentence.name, sentence.system)
    found = grading.compare(sentence.name, {"hypothesis": sentence.hypothesis, "proposed_fix": NO_FIX}, version)
    return {"verdict": found["verdict"], "rubric_version": version}


def _key(item: Sentence | Mapping) -> tuple:
    if isinstance(item, Sentence):
        return (item.name, item.system, item.hypothesis, item.expected)
    return (item["name"], item["system"], item["hypothesis"], item["expected"])


def judge_sentences(
    model: Model, sentences: Sequence[Sentence], done: Callable[[list[dict], str | None], None] = lambda *_: None
) -> tuple[list[dict], str | None]:
    """The judge on each sentence, one call each: its right answer for the system it was written for, and the
    hypothesis alone. It stops at the first call that gives no reply Klaxon can use. done gets what was judged so far,
    after each, and why it stopped. Returns both."""
    found: list[dict] = []
    for number, sentence in enumerate(sentences, 1):
        try:
            judgment = judge.judge(
                model, grading.answer(sentence.name, sentence.system), {"hypothesis": sentence.hypothesis}
            )
        except judge.NoReply as exc:
            stopped = f"at sentence {number}: {exc}"
            done(found, stopped)
            return found, stopped
        found.append(
            {"sentence": number, "name": sentence.name, "system": sentence.system, "hypothesis": sentence.hypothesis}
            | {"expected": sentence.expected, "source": sentence.source, "judgment": judgment.to_dict()}
        )
        done(found, None)
    return found, None


def _write(folder: Path, judged: list[dict], stopped: str | None = None) -> None:
    """calibration.json, whole or not at all: a temporary file, then moved into place."""
    value = {
        "label": LABEL,
        "judge": {"model": judge.JUDGE_MODEL, "version": judge.VERSION, "sha256": judge.fingerprint()},
        "sentences": judged,
        "stopped": stopped,
    }
    temporary = folder / f".{CALIBRATION}.writing"
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, folder / CALIBRATION)


def latest() -> Path | None:
    """The newest calibration folder in evidence/week5, or None."""
    found = sorted(path.parent for path in EVIDENCE.glob(f"*_calibration/{CALIBRATION}"))
    return found[-1] if found else None


# The report: offline.


def _agree(pairs: Sequence[tuple[object, object]]) -> dict:
    """How many of the pairs agree, of those where both sides gave a verdict."""
    both = [(a, b) for a, b in pairs if a is not None and b is not None and judge.NO_VERDICT not in (a, b)]
    return {"agree": sum(1 for a, b in both if a == b), "of": len(both)}


def _matrix(pairs: Sequence[tuple[str, str]]) -> dict:
    """Expected verdict by given verdict: how many of each."""
    table = {expected: {given: 0 for given in (*judge.VERDICTS, judge.NO_VERDICT)} for expected in judge.VERDICTS}
    for expected, given in pairs:
        table[expected][given] += 1
    return table


def _run_row(relative: str) -> dict:
    """One saved run by each grader: the rule's verdict worked out again; the judge's and Aravind's, each counted only
    when it graded this very diagnosis, against this right answer (and, the judge's, by the judge as committed)."""
    folder = REPO / relative
    item = grading.saved(folder)
    version = grading.newest_for(item.name, item.version)
    found = grading.compare(item.name, item.diagnosis, version)
    judged, judge_problem = judge.counted(folder, item)
    mine, label_problem = labels.counted(folder, item)
    return {
        "run": relative,
        "name": item.name,
        "system": item.version,
        "rule": {"verdict": found["verdict"], "fix_fits": found.get("fix_fits_the_remedy"), "rubric_version": version},
        "judge": None if judged is None else {key: judged["judgment"][key] for key in ("verdict", "fix_fits", "why")},
        "aravind": None if mine is None else {key: mine[key] for key in ("verdict", "fix_fits", "why")},
        "problems": [problem for problem in (judge_problem, label_problem) if problem is not None],
    }


def report(folder: Path | None) -> dict:
    """Each grader against the expected verdicts (all 66 sentences, judged or not yet) and against Aravind's (the
    saved runs): worked out from saved files, offline. The rule's verdicts are worked out again; the judge's and his
    are read."""
    saved = {} if folder is None else json.loads((folder / CALIBRATION).read_text(encoding="utf-8"))
    judged = {_key(item): item["judgment"] for item in saved.get("sentences", [])}
    stale = folder is not None and (saved.get("judge") or {}).get("sha256") != judge.fingerprint()
    if stale:  # another version of the judge: its judgments are not this one's (the re-check's finding L4)
        judged = {}
    sentences = [
        {"sentence": number, "name": s.name, "system": s.system, "hypothesis": s.hypothesis, "expected": s.expected}
        | {"source": s.source, "judgment": judged.pop(_key(s), None), "rule": rule_on(s)}
        for number, s in enumerate(SENTENCES, 1)
    ]
    unknown = [f"a judged sentence the tests no longer hold: {clean_text(key[2], 120)}" for key in judged]
    if stale:
        unknown.append("its judgments were made by another version of the judge: none is counted")
    runs = [_run_row(relative) for relative in SAVED_RUNS]
    labeled = [r for r in runs if r["aravind"] is not None]

    def verdict(side: Mapping | None) -> str | None:
        return None if side is None else side["verdict"]

    def fits(side: Mapping | None) -> bool | None:
        return None if side is None or side.get("verdict") == judge.NO_VERDICT else side.get("fix_fits")

    given = [(s["expected"], verdict(s["judgment"])) for s in sentences]
    return {
        "label": (
            "Worked out offline by klaxon.calibration from saved files: no model was called. The rule's verdicts are"
            " klaxon.grading's, worked out again; the judge's are Claude's, recorded; Aravind's were typed at a terminal"
            " (make label). The sentences' expected verdicts were written by Claude, for the tests."
        ),
        "from": None if folder is None else grading_shown(folder),
        "stopped": saved.get("stopped"),
        "problems": unknown,
        "sentences": sentences,
        "runs": runs,
        "agreement": {
            "sentences": {
                "count": len(sentences),
                "judged": sum(1 for _, by in given if by is not None),
                "rule_with_expected": _agree([(s["expected"], s["rule"]["verdict"]) for s in sentences]),
                "judge_with_expected": _agree(given),
                "judge_no_verdict": sum(1 for _, by in given if by == judge.NO_VERDICT),
                "rule_matrix": _matrix([(s["expected"], s["rule"]["verdict"]) for s in sentences]),
                "judge_matrix": _matrix([(expected, by) for expected, by in given if by is not None]),
            },
            "runs": {
                "count": len(runs),
                "judged": sum(1 for r in runs if r["judge"] is not None),
                "labeled": len(labeled),
                # On the runs he labeled only: nothing here tells him a grader's verdict before he gives his own.
                "rule_with_judge": _agree([(r["rule"]["verdict"], verdict(r["judge"])) for r in labeled]),
                "rule_with_aravind": _agree([(r["rule"]["verdict"], verdict(r["aravind"])) for r in labeled]),
                "judge_with_aravind": _agree([(verdict(r["judge"]), verdict(r["aravind"])) for r in labeled]),
                "rule_fix_with_aravind": _agree([(r["rule"]["fix_fits"], fits(r["aravind"])) for r in labeled]),
                "judge_fix_with_aravind": _agree([(fits(r["judge"]), fits(r["aravind"])) for r in labeled]),
            },
        },
    }


def grading_shown(path: Path) -> str:
    """A folder as evidence may name it: from the repo's root, or with this Mac's home folder written as ~."""
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return clean_text(str(path))


def _of(found: Mapping) -> str:
    return "-" if not found["of"] else f"{found['agree']} of {found['of']}"


def _cell(text: object) -> str:
    return clean_text(str(text), 300).replace("|", "/")


def markdown(found: Mapping) -> str:
    """The report as a page: the agreement, each grader's verdicts by the expected ones, every disagreement, then the
    saved runs, with both graders' verdicts held back on a run Aravind has not labeled yet."""
    said = found["agreement"]
    lines = ["# Klaxon's graders, checked", "", found["label"], ""]
    s = said["sentences"]
    lines += [
        f"## The {s['count']} sentences of the grading tests"
        + (f" (judged in {found['from']})" if found["from"] else ""),
        "",
        f"- The rule agrees with the expected verdict on {_of(s['rule_with_expected'])}.",
        f"- The judge agrees on {_of(s['judge_with_expected'])}; no verdict on {s['judge_no_verdict']};"
        f" judged {s['judged']} of {s['count']}.",
    ]
    if found["stopped"]:
        lines.append(f"- The calibration stopped {_cell(found['stopped'])}: the sentences after it were not judged.")
    for who in ("rule", "judge"):
        matrix = s[f"{who}_matrix"]
        lines += ["", f"The {who}'s verdicts (rows: the expected verdict):", ""]
        lines += ["| Expected | " + " | ".join(matrix[judge.VERDICTS[0]]) + " |", "|---" * 5 + "|"]
        lines += [
            f"| {expected} | " + " | ".join(str(n) for n in row.values()) + " |" for expected, row in matrix.items()
        ]
    wrong = [
        x
        for x in found["sentences"]
        if (x["judgment"] is not None and x["judgment"]["verdict"] != x["expected"])
        or x["rule"]["verdict"] != x["expected"]
    ]
    if wrong:
        lines += ["", "Where a grader differs from the expected verdict:", ""]
        lines += [
            f'- {x["sentence"]}. {_cell(x["name"])} (system of version {x["system"]}): "{_cell(x["hypothesis"])}"'
            f" expected {x['expected']}; rule {x['rule']['verdict']} (version {x['rule']['rubric_version']});"
            f" judge {'not judged yet' if x['judgment'] is None else _cell(judge.said(x['judgment']))}"
            for x in wrong
        ]
    r = said["runs"]
    lines += [
        "",
        f"## The {r['count']} saved diagnoses",
        "",
        f"Judged: {r['judged']} of {r['count']}. Labeled by Aravind: {r['labeled']} of {r['count']}. A run's verdicts"
        " are held back until he labels it.",
        "",
        f"- The rule and the judge agree on {_of(r['rule_with_judge'])} of the runs he labeled.",
        f"- The rule agrees with Aravind on {_of(r['rule_with_aravind'])}; on the fix, {_of(r['rule_fix_with_aravind'])}.",
        f"- The judge agrees with Aravind on {_of(r['judge_with_aravind'])}; on the fix,"
        f" {_of(r['judge_fix_with_aravind'])}.",
        "",
        "| Run | Graded as | Rule | Judge | Aravind |",
        "|---|---|---|---|---|",
    ]
    for row in found["runs"]:
        if row["aravind"] is None:
            cells = [HELD_BACK, HELD_BACK if row["judge"] is not None else "-", "-"]
        else:
            cells = [f"{row['rule']['verdict']} (v{row['rule']['rubric_version']}), fix {row['rule']['fix_fits']}"]
            for side in (row["judge"], row["aravind"]):
                cells.append("-" if side is None else f"{side['verdict']}, fix {side['fix_fits']}")
        lines.append(
            f"| {_cell(Path(row['run']).name)} | {_cell(row['name'])} | " + " | ".join(_cell(c) for c in cells) + " |"
        )
    problems = [(row["run"], problem) for row in found["runs"] for problem in row["problems"]]
    problems += [("the calibration", problem) for problem in found["problems"]]
    if problems:
        lines += ["", "Problems:", ""] + [f"- {_cell(run)}: {_cell(problem)}" for run, problem in problems]
    return "\n".join(lines) + "\n"


# Replay: offline.


def replay(folder: Path) -> list[str]:
    """Judge every sentence again from the calibration's recording, offline, and every saved run judged so far from
    its own: how they differ from what was saved ([] when all are reproduced). A calibration that stopped is asked for
    the sentence it stopped at too, and must stop there again."""
    saved = json.loads((folder / CALIBRATION).read_text(encoding="utf-8"))
    differences = []
    model = ReplayModel(folder / RECORDING)
    asked = [Sentence(x["name"], x["system"], x["hypothesis"], x["expected"], x["source"]) for x in saved["sentences"]]
    if saved.get("stopped"):
        asked.append(SENTENCES[len(asked)])  # the one it stopped at: its call gave no reply
    again, stopped = judge_sentences(model, asked)
    for before, after in zip(saved["sentences"], again, strict=False):
        if before != after:
            differences.append(f"sentence {before['sentence']}: the judgment differs")
    if len(again) != len(saved["sentences"]) or stopped != saved.get("stopped"):
        differences.append(f"it judged {len(again)} sentences and stopped {stopped}, not as recorded")
    if model.unused:
        differences.append(f"{model.unused} recorded call(s) were not asked for again")
    for relative in SAVED_RUNS:
        if (REPO / relative / judge.SAVED).is_file():
            differences += [f"{relative}: {d}" for d in judge.replay(REPO / relative)]
    return differences


# The command line.


def _run(argv: Sequence[str]) -> int:
    from klaxon.agent.claude_code import ClaudeCode, _provenance
    from klaxon.agent.model import Recording

    try:  # before any folder is made: a calibration that cannot start leaves nothing behind
        binary = find_live_model(judge.JUDGE_MODEL)
    except (ModelError, ValueError) as exc:
        print(f"Not started, and nothing was written: {clean_text(str(exc), 300)}", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    folder = loop._new_folder(EVIDENCE, f"{stamp}_calibration")
    header = {
        "what": WHAT,
        "rerun_command": clean_text(shlex.join(["python", "-m", "klaxon.calibration", *argv]), 2000),
        "judge": {"model": judge.JUDGE_MODEL, "version": judge.VERSION, "sha256": judge.fingerprint()},
        **_provenance(),
    }
    print(
        f"The calibration's folder: {clean_text(str(folder))}; {len(SENTENCES)} sentences, then {len(SAVED_RUNS)} saved"
        " runs",
        flush=True,
    )
    adapter = ClaudeCode(Recording(folder / RECORDING, header), model=judge.JUDGE_MODEL, binary=binary)
    try:
        judged, stopped = judge_sentences(adapter, SENTENCES, lambda found, why: _write(folder, found, why))
        none = sum(1 for x in judged if x["judgment"]["verdict"] == judge.NO_VERDICT)
        print(f"Sentences judged: {len(judged) - none}; no verdict: {none}.", flush=True)
        if stopped:
            print(f"Stopped {stopped}: no saved run was judged.", file=sys.stderr)
            return 1
        done = judge.judge_runs([REPO / relative for relative in SAVED_RUNS], binary, held_back=True)
    except KeyboardInterrupt:
        print(f"Interrupted: {CALIBRATION} and each {RECORDING} keep what was judged", file=sys.stderr)
        return 130
    print(f"Saved runs judged: {done.judged}; no verdict: {done.no_verdict}; passed over: {done.passed_over}.\n")
    print(markdown(report(folder)))
    if done.stopped:
        print(f"Stopped {done.stopped}: nothing after it was asked.", file=sys.stderr)
    return 0 if none == 0 and done.no_verdict == 0 and done.stopped is None else 1


def _folder(given: Path | None) -> Path | None:
    folder = given if given is not None else latest()
    if folder is not None and not (folder / CALIBRATION).is_file():
        raise ValueError(f"{clean_text(str(folder))} holds no {CALIBRATION}")
    return folder


def _report(given: Path | None, write: bool) -> int:
    try:
        folder = _folder(given)
        found = report(folder)
        if write and folder is not None:
            (folder / "calibration-report.md").write_text(markdown(found))
            if found["agreement"]["runs"]["labeled"] == found["agreement"]["runs"]["count"]:  # nothing held back
                (folder / "calibration-report.json").write_text(json.dumps(found, indent=2, ensure_ascii=False) + "\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Cannot make the report: {type(exc).__name__}: {clean_text(str(exc), 300)}", file=sys.stderr)
        return 2
    if folder is None:
        print("No calibration folder yet: the rule on the sentences, and the saved runs.\n")
    print(markdown(found))
    return 0


def _replay(given: Path | None) -> int:
    try:
        folder = _folder(given)
        if folder is None:
            raise ValueError("no calibration folder yet in evidence/week5")
        differences = replay(folder)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"Cannot replay: {type(exc).__name__}: {clean_text(str(exc), 300)}", file=sys.stderr)
        return 2
    if differences:
        print("Not reproduced:\n" + "\n".join(f"  {d}" for d in differences))
        return 1
    print("Reproduced: the same prompts, replies and judgments as recorded, for every sentence and saved run judged.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="python -m klaxon.calibration", description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("run", help="live: the judge on every sentence and saved run; Claude on Aravind's seat")
    shown = actions.add_parser("report", help="each grader against the expected verdicts and Aravind's; offline")
    again = actions.add_parser("replay", help="the recorded judgments again, offline")
    for sub in (shown, again):
        sub.add_argument("folder", type=Path, nargs="?", help="a calibration folder (the newest by default)")
    shown.add_argument("--write", action="store_true", help="also write the report into the calibration's folder")
    args = parser.parse_args(argv)
    if args.action == "report":
        return _report(args.folder, args.write)
    if args.action == "replay":
        return _replay(args.folder)
    return _run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
