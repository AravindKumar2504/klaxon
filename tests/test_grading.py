"""The rule that compares a diagnosis with the cause a fault records. The hypotheses here were written for the tests by
Claude (the sessions that built each rubric, and their independent reviews), with the verdict each expects: not Klaxon's
output, and not Aravind's verdicts. The saved runs' hypotheses are the exception (the first live run's, a recorded
response from Claude, claude-opus-5-5, 2026-10-18, and the fault runs'), read from their evidence as data. The 66
sentences with an expected verdict are kept in klaxon.calibration, where the judge is checked on them too (week 5, item
2)."""

import json
import re
from pathlib import Path

import pytest

from klaxon import alerts, calibration, evaluation, faults, grading
from klaxon.agent.tools import HOME
from klaxon.agent.trace import read_trace
from klaxon.patient.stack import REPO

FIRST_LIVE_RUN = REPO / "evidence" / "week3" / "2026-10-18T112951Z_loop_ledgerlaghigh" / "trace.jsonl"


def diagnosis(hypothesis, kind="rollback", target="ledger-consumer", **extra):
    return {
        "hypothesis": hypothesis,
        "evidence": [{"call_id": "c1", "tool": "recent_deploys", "arguments": {}, "shows": "x"}],
        "confidence": 0.8,
        "proposed_fix": {"kind": kind, "target": target, "summary": "x", "applied": False},
        "blast_radius": "x",
        "rollback_plan": "x",
    } | extra


def verdict(fault, hypothesis, version=grading.RUBRIC_VERSION):
    return grading.compare(fault, diagnosis(hypothesis), version)["verdict"]


VERSIONS = sorted(grading.VERSIONS)
FIRST_THREE = ("duplicate-delivery", "poison-message", "provider-outage")
# The faults each version grades: versions 1 and 2 came before faults 4 to 6 (week 4, item 4), and version 4 also
# grades the evaluation's two new cases (week 5, item 2).
CASES = ("false-alarm", "missing-deploys")
GRADES = {1: FIRST_THREE, 2: FIRST_THREE, 3: tuple(faults.FAULTS), 4: (*faults.FAULTS, *CASES)}
GRADED = [(name, version) for version in VERSIONS for name in GRADES[version]]
PAIRS = [(name, other, version) for version in VERSIONS for name in GRADES[version] for other in GRADES[version]]


@pytest.mark.parametrize("version", VERSIONS)
def test_every_fault_has_a_rubric_with_three_required_elements(version):
    assert set(grading.VERSIONS[version]) == set(GRADES[version])
    assert set(grading.VERSIONS[grading.RUBRIC_VERSION]) == {*faults.FAULTS, *CASES}  # the latest grades every case
    for rubric in grading.VERSIONS[version].values():
        assert sum(element.required for element in rubric.elements) == 3
        for element in (*rubric.elements, *rubric.wrong, *rubric.also):
            for pattern in (*element.patterns, *element.unless):
                re.compile(pattern)


@pytest.mark.parametrize("name, version", GRADED)
def test_each_faults_own_recorded_cause_names_the_cause(name, version):
    found = grading.compare(name, diagnosis(grading.answer(name, version).cause), version)
    assert found["verdict"] == "names the cause"
    assert all(item["named"] for item in found["elements"])


# Missing-deploys' right answer holds fault 1's cause, word for word: it names fault 1, and more.
OVERLAP = {("missing-deploys", "duplicate-delivery")}


@pytest.mark.parametrize("name, other, version", [(a, b, v) for a, b, v in PAIRS if a != b and (a, b) not in OVERLAP])
def test_no_faults_cause_names_another_faults_cause(name, other, version):
    assert verdict(other, grading.answer(name, version).cause, version) != "names the cause"


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("name", sorted(set(faults.FAULTS) - set(FIRST_THREE)))
def test_a_version_from_before_a_fault_refuses_to_grade_it(name, version):
    with pytest.raises(ValueError, match=f"rubric version {version} has no rubric for {name}"):
        grading.compare(name, diagnosis("x"), version)


@pytest.mark.parametrize("version", VERSIONS)
def test_the_first_live_runs_wrong_diagnosis_misses_every_faults_cause(version):
    # It called an idle ledger consumer "hung or silently stalled": that restates the alert, and names no cause.
    recorded = read_trace(FIRST_LIVE_RUN)[-1]["diagnosis"]
    for name in GRADES[version]:
        assert grading.compare(name, recorded, version)["verdict"] == "misses the cause", name


def sentences(source):
    """The sentences of one source in klaxon.calibration, as (fault, hypothesis, expected)."""
    return [(s.name, s.hypothesis, s.expected) for s in calibration.SENTENCES if s.source == source]


@pytest.mark.parametrize("fault, hypothesis, expected", sentences(calibration.V1_TESTS))
def test_hypotheses_written_for_the_test_get_the_expected_verdict(fault, hypothesis, expected):
    assert verdict(fault, hypothesis, version=1) == expected
    if fault != "provider-outage":  # version 2 changed only fault 3
        assert verdict(fault, hypothesis, version=2) == expected


@pytest.mark.parametrize("hypothesis, expected", [(h, e) for _, h, e in sentences(calibration.V2_TESTS)])
def test_version_2_hypotheses_written_for_the_test_get_the_expected_verdict(hypothesis, expected):
    assert verdict("provider-outage", hypothesis, version=2) == expected


@pytest.mark.parametrize("version", VERSIONS)
def test_the_topic_named_after_the_provider_is_not_the_provider(version):
    assert verdict("provider-outage", "Messages on provider-events time out and threads wait.", 1) == "partly"
    assert verdict("provider-outage", "Messages on provider-events time out; the breaker opened.", 2) == "partly"
    where = grading.VERSIONS[version]["provider-outage"].elements[0]
    assert not grading.names(where, "the provider_events topic")


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("words", ["timeouts", "times out", "timed out", "is timing out", "a time-out"])
def test_every_way_of_saying_a_timeout_counts(words, version):
    assert grading.names(grading.VERSIONS[version]["provider-outage"].elements[1], f"the provider {words}")


@pytest.mark.parametrize(
    "words",
    ["circuit breaker", "circuit-breaker", "the breaker opened", "fails fast", "short-circuits", "circuit is open"],
)
def test_every_way_of_saying_the_breaker_fails_fast_counts_in_version_2(words):
    assert grading.names(grading.VERSIONS[2]["provider-outage"].elements[2], f"payments-api {words}")


def test_matching_ignores_case():
    assert verdict("poison-message", "THE LEDGER RETRIES A MALFORMED EVENT FOREVER") == "names the cause"


def test_only_the_hypothesis_is_read():
    # The evidence and the fix name everything; the hypothesis names nothing: the cause was not named.
    found = grading.compare(
        "poison-message",
        diagnosis(
            "Something is wrong.",
            target="ledger-consumer: roll back BAD_EVENTS=retry, it retries a malformed event forever",
            evidence=[{"call_id": "c1", "shows": "the ledger retries a poison message forever"}],
        ),
    )
    assert found["verdict"] == "misses the cause"


@pytest.mark.parametrize(
    "fault, kind, target, fits",
    [
        ("duplicate-delivery", "rollback", "ledger-consumer", True),
        ("duplicate-delivery", "config_change", "the ledger consumer", True),
        ("duplicate-delivery", "restart", "ledger-consumer", False),
        ("duplicate-delivery", "rollback", "payments-api", False),
        ("poison-message", "rollback", "ledger-consumer", True),
        ("poison-message", "data_repair", "ledger-consumer", False),
        ("provider-outage", "none", "fake-provider", True),
        ("provider-outage", "code_change", "payments-api", True),
        ("provider-outage", "rollback", "payments-api", False),
    ],
)
def test_whether_the_proposed_fix_fits_the_faults_remedy(fault, kind, target, fits):
    found = grading.compare(fault, diagnosis("x", kind=kind, target=target), version=1)
    assert found["fix_fits_the_remedy"] is fits


@pytest.mark.parametrize(
    "kind, target, fits",
    [
        ("none", "fake-provider", True),  # nothing to change on our side
        ("other", "the payment provider: escalate", True),
        ("none", "payments-api", True),
        ("code_change", "payments-api", False),  # the breaker is already there
        ("config_change", "payments-api", False),
        ("rollback", "payments-api", False),
        ("restart", "payments-api", False),
        ("other", "ledger-consumer", False),
    ],
)
def test_in_version_2_only_no_change_or_escalating_fits_the_outage(kind, target, fits):
    found = grading.compare("provider-outage", diagnosis("x", kind=kind, target=target), version=2)
    assert found["fix_fits_the_remedy"] is fits


def test_a_run_without_a_diagnosis_has_nothing_to_compare():
    found = grading.compare("provider-outage", None)
    assert found["verdict"] == "no diagnosis" and found["recorded_cause"] == faults.FAULTS["provider-outage"].cause
    assert grading.render(found)[-1] == "  verdict: no diagnosis to compare"


@pytest.mark.parametrize("version", VERSIONS)
def test_the_comparison_always_says_a_rule_made_it_not_a_person_or_a_model(version):
    found = grading.compare("poison-message", diagnosis("x"), version)
    assert found["by"] == grading.BY[version] and "Not a person and not a model" in found["by"]
    assert f"rubric version {version}," in found["by"]
    assert grading.render(found)[0] == f"Compared by {grading.BY[version]}"


def test_the_printed_comparison_cleans_the_models_words():
    found = grading.compare("poison-message", diagnosis(f"The ledger​ retries {HOME}/x forever", target="‮led"))
    printed = "\n".join(grading.render(found))
    assert "​" not in printed and "‮" not in printed and HOME not in printed
    assert "  hypothesis: The ledger retries ~/x forever" in printed
    assert printed.endswith("  verdict: PARTLY")


# After the independent review (DECISIONS.md lists its findings by number)

FAULT_RUNS = REPO / "evidence" / "week3"
GATED_RUNS = REPO / "evidence" / "week4"


def test_the_rubric_is_version_1_exactly_as_committed_before_the_live_runs():
    # Finding 2. Any change to an element, a pattern or a fitting fix changes this fingerprint. Then: a new version,
    # its reason in DECISIONS.md, and the verdicts already recorded kept as they were.
    assert grading.fingerprint(1) == "81fd17fd69a6c0cf5086e59455babf2d736b9cedb7c9191c9ce68894f3323dfb"
    assert grading.compare("poison-message", diagnosis("x"), version=1)["rubric"] == {
        "version": 1,
        "sha256": grading.fingerprint(1),
    }
    assert "rubric version 1, committed in 95a64ab before the first live runs on the faults" in grading.BY[1]


def test_version_2_is_version_1_but_for_fault_3():
    # Week 4, item 3: committed before any live run on fault 3 with the circuit breaker. Pinned the same way.
    assert grading.fingerprint(2) == "3aafea2a2d77b934fada2e8817d8e693e23e1a895e9eeacea41cd366a34356c9"
    assert {name: rubric for name, rubric in grading.VERSIONS[2].items() if name != "provider-outage"} == {
        name: rubric for name, rubric in grading.VERSIONS[1].items() if name != "provider-outage"
    }
    assert grading.VERSIONS[2]["provider-outage"] != grading.VERSIONS[1]["provider-outage"]
    found = grading.compare("provider-outage", diagnosis("x"), version=2)
    assert found["rubric"] == {"version": 2, "sha256": grading.fingerprint(2)}
    assert (found["recorded_cause"], found["remedy"]) == (
        faults.FAULTS["provider-outage"].cause,
        faults.FAULTS["provider-outage"].remedy,
    )
    assert (
        "rubric version 2, committed in 8e58e7f before any live run on fault 3 with the circuit breaker"
        in grading.BY[2]
    )


def test_version_3_is_version_2_and_a_rubric_for_each_of_faults_4_to_6():
    # Week 4, item 4: committed before any live run on faults 4 to 6. Pinned the same way.
    assert grading.fingerprint(3) == V3_FINGERPRINT
    assert {name: grading.VERSIONS[3][name] for name in FIRST_THREE} == grading.VERSIONS[2]
    assert set(grading.VERSIONS[3]) - set(FIRST_THREE) == {"money-unit", "secret-rotation", "pool-exhaustion"}
    for name in faults.FAULTS:
        found = grading.compare(name, diagnosis("x"), version=3)
        assert found["rubric"] == {"version": 3, "sha256": grading.fingerprint(3)}
        assert (found["recorded_cause"], found["remedy"]) == (faults.FAULTS[name].cause, faults.FAULTS[name].remedy)
        assert "wrong_causes" not in found and all("cancelled" not in item for item in found["elements"])
    assert "rubric version 3, committed in 75fedf1 before any live run on faults 4 to 6" in grading.BY[3]


V3_FINGERPRINT = "9cd4375a58cc3b1cb2b7a2da52835d398b24c544a14855cc2f0b72b56f19d4c7"  # pinned before any live run


# Version 3's sentences: written for the tests (not Klaxon's output), with the independent review's and its re-check's.
V3_SENTENCES = sentences(calibration.V3_TESTS)


@pytest.mark.parametrize("fault, hypothesis, expected", V3_SENTENCES)
def test_version_3_hypotheses_written_for_the_test_get_the_expected_verdict(fault, hypothesis, expected):
    assert verdict(fault, hypothesis, version=3) == expected


@pytest.mark.parametrize(
    "words, named",
    [
        ("the receiver answers with a 400", True),
        ("it returns 400 Bad Request", True),
        ("HTTP 400", True),
        ("400 of the provider's webhooks", False),
        ("a 400 ms wait", False),
    ],
)
def test_in_version_3_a_400_counts_only_as_an_answer(words, named):
    how = grading.VERSIONS[3]["secret-rotation"].elements[2]
    assert grading.names(how, words) is named


@pytest.mark.parametrize(
    "fault, kind, target, fits",
    [
        ("money-unit", "rollback", "payments-api", True),
        ("money-unit", "config_change", "payments-api", True),
        ("money-unit", "data_repair", "payments-api", False),  # the overcharges need repair, but that is not the fix
        ("money-unit", "rollback", "ledger-consumer", False),
        ("secret-rotation", "rollback", "webhook-receiver", True),
        ("secret-rotation", "config_change", "webhook-receiver", True),
        ("secret-rotation", "none", "fake-provider", False),
        ("secret-rotation", "rollback", "payments-api", False),
        ("pool-exhaustion", "rollback", "payments-api", True),
        ("pool-exhaustion", "restart", "payments-api", False),  # a fresh pool leaks again
        ("pool-exhaustion", "scale", "postgres", False),
    ],
)
def test_in_version_3_whether_the_proposed_fix_fits_faults_4_to_6(fault, kind, target, fits):
    found = grading.compare(fault, diagnosis("x", kind=kind, target=target), version=3)
    assert found["fix_fits_the_remedy"] is fits


def test_a_comparison_by_version_1_reads_the_cause_fault_3_recorded_then():
    found = grading.compare("provider-outage", diagnosis("x"), version=1)
    then = faults.FAULTS["provider-outage"].recorded(1)
    assert (found["recorded_cause"], found["remedy"]) == (then.cause, then.remedy)
    assert "has no circuit breaker" in then.cause and "circuit breaker opens" in faults.FAULTS["provider-outage"].cause
    assert faults.FAULTS["duplicate-delivery"].recorded(1).cause == faults.FAULTS["duplicate-delivery"].cause


@pytest.mark.parametrize(
    "saved, version",
    [
        (None, 4),  # no comparison saved: the latest
        ({"verdict": "names the cause"}, 1),  # saved before comparisons recorded a version: the week 3 runs
        ({"rubric": {"version": 1, "sha256": "x"}}, 1),
        ({"rubric": {"version": 2, "sha256": "x"}}, 2),
        ({"rubric": {"version": 3, "sha256": "x"}}, 3),
        ({"rubric": {"version": 4, "sha256": "x"}}, 4),
    ],
)
def test_a_saved_comparison_is_graded_again_with_its_own_version(saved, version):
    assert grading.version_of(saved) == version


@pytest.mark.parametrize("rubric", [{"version": 9}, {"version": "1"}, {"version": True}, "1", {}, None])
def test_a_saved_comparison_naming_a_version_this_code_does_not_have_is_refused(rubric):
    with pytest.raises(ValueError, match="does not have"):
        grading.version_of({"rubric": rubric})


@pytest.mark.parametrize("saved", [[], "v1", 1])
def test_a_saved_comparison_that_is_not_an_object_is_refused(saved):
    with pytest.raises(ValueError, match="not an object"):
        grading.version_of(saved)


# The rules' own sentences for each fault's symptoms. Faults 4 to 6's are the rules' sentences for what those faults are
# built to show; their live runs' alerts join the ones read from the evidence below.
MADE = [
    (
        "duplicate-delivery",
        alerts.money(
            {"outcome": "failed", "invariants": 5, "failed": {"posted_exactly_once": 7}, "money_off_cents": 1}
        ).sentence,
    ),
    ("poison-message", alerts.lags([({"consumer": "ledger"}, 271.0)])[0].sentence),
    ("poison-message", alerts.books({"outcome": "could not settle", "messages_behind": {"ledger": 124}}).sentence),
    ("provider-outage", f"{alerts.errors(2.2, 2.97).sentence} {alerts.latency(10.0).sentence}"),
    (
        "money-unit",
        alerts.money(
            {"outcome": "failed", "invariants": 5, "failed": {"payments_match_provider": 40}, "money_off_cents": 0}
        ).sentence,
    ),
    ("secret-rotation", alerts.books({"outcome": "could not settle", "webhooks_not_acknowledged": 52}).sentence),
    ("pool-exhaustion", f"{alerts.errors(3.0, 3.0).sentence} {alerts.latency(5.0).sentence}"),
    # The evaluation's two cases: the false alarm's alert, in the lag rule's words, and fault 1's, for missing-deploys.
    ("false-alarm", evaluation.false_alert("2026-11-04T00:00:00Z").summary),
    (
        "missing-deploys",
        alerts.money(
            {"outcome": "failed", "invariants": 5, "failed": {"posted_exactly_once": 7}, "money_off_cents": 1}
        ).sentence,
    ),
]


@pytest.mark.parametrize("version", VERSIONS)
def test_an_alerts_own_words_never_name_enough_of_its_faults_cause(version):
    # Finding 2: restating the alert is not a diagnosis. The alerts the live runs raised (recorded in their run.json),
    # each read as a hypothesis, miss their fault's cause; so do the rules' sentences for those symptoms.
    runs = [*FAULT_RUNS.glob("*_fault-run_*/run.json"), *GATED_RUNS.glob("*_gated-run_*/run.json")]
    saved = [json.loads(path.read_text()) for path in sorted(runs)]
    recorded = [(run["fault"]["name"], run["alert"]["summary"]) for run in saved if run["alert"] is not None]
    assert set(FIRST_THREE) <= {fault for fault, _ in recorded}  # a run that stopped before its alert has none
    assert {fault for fault, _ in MADE} == set(GRADES[grading.RUBRIC_VERSION])
    for fault, sentence in [*recorded, *MADE]:
        if fault in GRADES[version]:
            assert verdict(fault, sentence, version) == "misses the cause", (fault, sentence)


# Finding 2's examples: what a person would say, and version 1 does not. Each is a known limit, kept as a test that
# fails today (strict: one that starts to pass fails too), for the week 5 grader to do better on.
KNOWN_LIMITS = sentences(calibration.V1_LIMIT)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a known limit of rubric version 1 (finding 2): it reads words, not meaning",
)
@pytest.mark.parametrize("fault, hypothesis, right", KNOWN_LIMITS)
def test_a_known_limit_of_version_1(fault, hypothesis, right):
    assert verdict(fault, hypothesis, version=1) == right


# Version 2's own: in a system with the breaker, a hypothesis that says there is none names the word, and is wrong.
KNOWN_LIMITS_V2 = [(h, e) for _, h, e in sentences(calibration.V2_LIMIT)]


# Version 3's own (the independent review's sentence): a wrong cause that names every element. It blames the payment
# confirmer, but says "a unit bug", "the amounts differ" and, in passing, "the payments API's records".
KNOWN_LIMITS_V3 = sentences(calibration.V3_LIMIT)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a known limit of rubric version 3: it reads words, not meaning",
)
@pytest.mark.parametrize("fault, hypothesis, right", KNOWN_LIMITS_V3)
def test_a_known_limit_of_version_3(fault, hypothesis, right):
    assert verdict(fault, hypothesis, version=3) == right


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a known limit of rubric version 2: it reads words, not meaning",
)
@pytest.mark.parametrize("hypothesis, right", KNOWN_LIMITS_V2)
def test_a_known_limit_of_version_2(hypothesis, right):
    assert verdict("provider-outage", hypothesis, version=2) == right


# Version 4 (week 5, item 2): the nine known limits fixed where a rule can, words that cancel an element (unless), known
# wrong causes (wrong), and the evaluation's two cases. Its sentences below were written for these tests by Claude, not
# Klaxon. Its new patterns were written knowing the known limits: these tests show each mechanism works, nothing more.

V4_FINGERPRINT = "88e724b0e5915247a6c9fab35b00d26b2720e5818ee5b7be955feacb3c70112c"  # pinned before any week 5 live run
CHANGED_IN_4 = ("duplicate-delivery", "poison-message", "provider-outage", "money-unit")
V1_OUTAGE = {
    h for f, h, _ in sentences(calibration.V1_TESTS) if f == "provider-outage"
}  # written for version 1's system


def test_version_4_is_version_3_with_its_limits_fixed_and_two_new_cases_and_new_comparisons_use_it():
    assert grading.RUBRIC_VERSION == 4 and grading.RUBRICS is grading.VERSIONS[4]
    assert grading.fingerprint() == grading.fingerprint(4) == V4_FINGERPRINT
    assert set(grading.VERSIONS[4]) == {*faults.FAULTS, *CASES}
    for name in set(faults.FAULTS) - set(CHANGED_IN_4):  # no known limit: unchanged
        assert grading.VERSIONS[4][name] == grading.VERSIONS[3][name]
    for name in CHANGED_IN_4:  # every pattern of version 3 kept, and added to
        old, new = grading.VERSIONS[3][name], grading.VERSIONS[4][name]
        assert (old.fix_kinds, old.fix_target) == (new.fix_kinds, new.fix_target) and new != old
        for before, after in zip(old.elements, new.elements, strict=True):
            assert (before.name, before.required) == (after.name, after.required)
            assert set(before.patterns) <= set(after.patterns)
    for name in GRADES[4]:
        found = grading.compare(name, diagnosis("x"))
        right = grading.answer(name)
        assert found["rubric"] == {"version": 4, "sha256": grading.fingerprint(4)}
        assert (found["recorded_cause"], found["remedy"]) == (right.cause, right.remedy)
        assert found["wrong_causes"] == [] == found["wrong_causes_offered"]
        assert all(item["cancelled"] is False for item in found["elements"])
    assert "rubric version 4, committed" in grading.BY[4] and "before any week 5 live run" in grading.BY[4]


def test_version_4s_new_fields_are_left_out_of_an_earlier_versions_fingerprint_where_they_are_empty():
    # So the fingerprints pinned before the earlier versions' runs still hold (the tests above pin each).
    for version in (1, 2, 3):
        for rubric in grading.VERSIONS[version].values():
            shape = grading._shape(rubric)
            assert "wrong" not in shape and "also" not in shape
            assert all("unless" not in element and "scoped" not in element for element in shape["elements"])
    shape = grading._shape(grading.VERSIONS[4]["money-unit"])
    assert shape["elements"][0]["unless"] and "unless" not in shape["elements"][1]
    shape = grading._shape(grading.VERSIONS[4]["missing-deploys"])
    assert shape["wrong"] and shape["also"] and "scoped" not in shape["also"][0]  # its own words are negations


@pytest.mark.parametrize(
    "fault, hypothesis, right",
    [*KNOWN_LIMITS, *(("provider-outage", h, r) for h, r in KNOWN_LIMITS_V2), *KNOWN_LIMITS_V3],
)
def test_version_4_gets_each_known_limit_right(fault, hypothesis, right):
    # Two were written for fault 3 before the breaker: they blame the database or a traffic spike, wrong in either
    # system. Version 4 grades them in its own.
    assert verdict(fault, hypothesis, version=4) == right


@pytest.mark.parametrize(
    "fault, hypothesis, expected",
    [
        *((f, h, e) for f, h, e in sentences(calibration.V1_TESTS) if h not in V1_OUTAGE),
        *sentences(calibration.V2_TESTS),
        *V3_SENTENCES,
    ],
)
def test_version_4_keeps_every_earlier_sentences_verdict(fault, hypothesis, expected):
    assert verdict(fault, hypothesis, version=4) == expected


@pytest.mark.parametrize(
    "fault, hypothesis, expected",
    [
        # Fault 1: Redis losing its keys is another cause, in any word order; a denial is not one.
        (
            "duplicate-delivery",
            "The ledger consumer's dedupe keys were evicted from Redis, so webhooks the provider sends again are booked"
            " twice.",
            "misses the cause",
        ),
        (
            "duplicate-delivery",
            "Redis did not lose any keys: a deploy turned off the ledger consumer's dedupe, so webhooks the provider"
            " sends again are booked twice.",
            "names the cause",
        ),
        (
            "duplicate-delivery",
            "A deploy switched off the ledger consumer's duplicate detection, so redelivered webhooks are booked twice.",
            "names the cause",
        ),
        # Fault 2: a database outage is another cause; a denial is not one; "a decimal string" is its message.
        (
            "poison-message",
            "While the database was unavailable, the ledger consumer kept retrying one malformed event forever.",
            "misses the cause",
        ),
        (
            "poison-message",
            "The ledger consumer retries one malformed event forever; PostgreSQL was not down.",
            "names the cause",
        ),
        (
            "poison-message",
            "The ledger consumer retries forever one event whose amount was a decimal string.",
            "names the cause",
        ),
        # Fault 3, with the breaker: a breaker denied is not the breaker failing fast; a slow database is another cause.
        ("provider-outage", "The provider hangs, and payments-api runs without a breaker.", "partly"),
        ("provider-outage", "The provider hangs, and the breaker is disabled (PROVIDER_BREAKER=off).", "partly"),
        (
            "provider-outage",
            "PostgreSQL is slow, so payments-api waits; the provider times out and our breaker opened.",
            "misses the cause",
        ),
        (
            "provider-outage",
            "PostgreSQL was not slow: the provider times out, and the breaker opened.",
            "names the cause",
        ),
        # Fault 4: a bug put in another service of ours does not name the payments API, though it is mentioned.
        (
            "money-unit",
            "A deploy of the ledger consumer has a unit bug: it reads the provider's amounts as dollars, so the amounts"
            " differ from the payments API's records.",
            "partly",
        ),
        (
            "money-unit",
            "A deploy of payments-api reads cents as dollars, so the provider charges 100 times our amount; the payment"
            " confirmer then marks them succeeded.",
            "names the cause",
        ),
    ],
)
def test_version_4s_unless_and_wrong_causes_each_work(fault, hypothesis, expected):
    assert verdict(fault, hypothesis, version=4) == expected


FALSE_ALARM_SENTENCES = [
    (
        "The alert is false: the ledger consumer group's lag reads 0 under traffic, so nothing is behind.",
        "names the cause",
    ),
    ("A false alarm: the ledger group is caught up, with no consumer lag.", "names the cause"),
    ("Nothing is wrong: the ledger consumer keeps up.", "names the cause"),
    ("The ledger consumer is not stalled: its lag is 0, so the alert is false.", "names the cause"),
    ("The ledger consumer group's lag is 0.", "partly"),  # it does not say the alert is false
    ("The ledger consumer appears hung or silently stalled.", "misses the cause"),
    ("The alert is false, but the ledger consumer is stuck on a poison message.", "misses the cause"),  # a real fault
    ("The ledger consumer group is 250 messages behind on provider-events.", "misses the cause"),  # the alert, restated
]


@pytest.mark.parametrize("hypothesis, expected", FALSE_ALARM_SENTENCES)
def test_the_false_alarms_rubric_asks_that_the_alert_be_called_false_from_its_lag(hypothesis, expected):
    assert verdict("false-alarm", hypothesis, 4) == expected


MISSING_DEPLOYS_SENTENCES = [
    (
        "The ledger consumer books every webhook the provider sends again: its dedupe is gone. Docker did not answer,"
        " so I could not check the deploy list.",
        "names the cause",
    ),
    (
        "The ledger consumer double-books redelivered webhooks: its idempotency check is gone. That a deploy did it is"
        " unverified: service_health and recent_deploys failed.",
        "names the cause",
    ),
    # The cause, and nothing of what it could not check.
    (
        "The ledger consumer books every webhook the provider sends again: its dedupe is gone, after a deploy.",
        "partly",
    ),
    # Saying what it could not check never lifts a wrong cause.
    ("The ledger consumer appears hung; I could not check the deploy list.", "misses the cause"),
    (
        "Redis lost its keys, so the ledger books webhooks the provider sends again; recent_deploys timed out.",
        "misses the cause",
    ),
]


@pytest.mark.parametrize("hypothesis, expected", MISSING_DEPLOYS_SENTENCES)
def test_missing_deploys_needs_fault_1s_cause_and_what_it_could_not_check(hypothesis, expected):
    assert verdict("missing-deploys", hypothesis, 4) == expected


def test_what_it_could_not_check_is_marked_as_required_only_to_name_the_cause():
    found = grading.compare("missing-deploys", diagnosis(MISSING_DEPLOYS_SENTENCES[2][0]))
    last = found["elements"][-1]
    assert last["only_to_name_the_cause"] is True and last["named"] is False and last["required"] is True
    assert all("only_to_name_the_cause" not in item for item in found["elements"][:-1])
    printed = grading.render(found)
    assert any("not named (required to name the cause) what it could not check" in line for line in printed)
    assert printed[-1] == "  verdict: PARTLY"


def test_a_comparison_by_version_4_says_which_words_were_cancelled_and_which_wrong_cause_it_names():
    found = grading.compare("money-unit", diagnosis(KNOWN_LIMITS_V3[0][1]))
    assert [item["cancelled"] for item in found["elements"]] == [True, False, False, False]
    assert found["verdict"] == "partly" and found["wrong_causes"] == []
    printed = "\n".join(grading.render(found))
    assert "not named (required) where: the payments API (its words are there, but put elsewhere or denied)" in printed
    found = grading.compare("duplicate-delivery", diagnosis(KNOWN_LIMITS[1][1]))
    assert found["wrong_causes"] == ["Redis lost the keys"] and found["verdict"] == "misses the cause"
    assert "  names a known wrong cause: Redis lost the keys" in grading.render(found)
    hedged = "The ledger consumer's dedupe is gone, so webhooks the provider re-sends are booked twice; or perhaps Redis lost keys."
    found = grading.compare("duplicate-delivery", diagnosis(hedged))
    assert found["wrong_causes_offered"] == ["Redis lost the keys"] and found["verdict"] == "partly"
    assert "  offers a known wrong cause as a maybe: Redis lost the keys" in grading.render(found)


@pytest.mark.parametrize(
    "case, kind, target, fits",
    [
        ("false-alarm", "none", "ledger-consumer", True),  # nothing to fix, on any target
        ("false-alarm", "none", "nothing", True),
        ("false-alarm", "restart", "ledger-consumer", False),
        ("false-alarm", "other", "the alert rule", False),
        ("missing-deploys", "rollback", "ledger-consumer", True),  # as fault 1
        ("missing-deploys", "config_change", "the ledger consumer", True),
        ("missing-deploys", "restart", "ledger-consumer", False),
    ],
)
def test_in_version_4_whether_the_proposed_fix_fits_the_two_new_cases(case, kind, target, fits):
    assert grading.compare(case, diagnosis("x", kind=kind, target=target))["fix_fits_the_remedy"] is fits


def test_the_two_cases_right_answers_come_from_the_evaluations_cases():
    for name in CASES:
        case = evaluation.CASES[name]
        assert grading.answer(name) == faults.Recorded(case.right_answer, case.remedy)
    assert grading.answer("missing-deploys").remedy == faults.FAULTS["duplicate-delivery"].remedy
    assert grading.answer("false-alarm").remedy == evaluation.NOTHING_TO_FIX
    assert grading.answer("provider-outage", 1) == faults.FAULTS["provider-outage"].recorded(1)
    with pytest.raises(ValueError, match="no fault or evaluation case 'nope'"):
        grading.answer("nope")
    with pytest.raises(ValueError, match="no fault or evaluation case 'nope'"):
        grading.compare("nope", diagnosis("x"))
    with pytest.raises(ValueError, match="rubric version 3 has no rubric for false-alarm"):
        grading.compare("false-alarm", diagnosis("x"), 3)


@pytest.mark.parametrize(
    "name, version, newest",
    [
        ("duplicate-delivery", 1, 4),  # its cause never changed
        ("poison-message", 2, 4),
        ("provider-outage", 1, 1),  # before the breaker: only version 1 has that system
        ("provider-outage", 2, 4),
        ("money-unit", 3, 4),
        ("false-alarm", 4, 4),
    ],
)
def test_a_saved_run_is_graded_again_with_the_newest_version_written_for_its_system(name, version, newest):
    assert grading.newest_for(name, version) == newest


def test_newest_for_refuses_a_version_with_no_rubric_for_the_name():
    with pytest.raises(ValueError, match="rubric version 1 has no rubric for money-unit"):
        grading.newest_for("money-unit", 1)


SAVED_RUNS = [REPO / relative for relative in calibration.SAVED_RUNS]


def test_the_13_saved_diagnoses_by_the_newest_rubric_for_their_system():
    # The verdicts the rule gives the calibration's saved runs: every fault run names its cause, as recorded when it
    # ran (week 3's fault 3 by version 1, the only rubric for its system); week 3's first live run misses the false
    # alarm's, and its restart does not fit.
    found = {}
    for folder in SAVED_RUNS:
        item = grading.saved(folder)
        found[folder.name] = grading.compare(item.name, item.diagnosis, grading.newest_for(item.name, item.version))
    first = found.pop("2026-10-18T112951Z_loop_ledgerlaghigh")
    assert first["fault"] == "false-alarm" and first["rubric"]["version"] == 4
    assert first["verdict"] == "misses the cause" and first["fix_fits_the_remedy"] is False
    assert first["wrong_causes"] == ["a real fault in the ledger consumer (stuck, hung, stalled, crashed)"]
    assert len(found) == 12 and {c["verdict"] for c in found.values()} == {"names the cause"}
    assert found["2026-10-19T024001Z_fault-run_provider-outage"]["rubric"]["version"] == 1
    assert {c["rubric"]["version"] for name, c in found.items() if "024001Z" not in name} == {4}


def test_saved_reads_a_runs_fault_its_version_and_its_diagnosis(tmp_path):
    item = grading.saved(REPO / calibration.SAVED_RUNS[3])  # week 3's fault 3, graded by version 1
    assert (item.name, item.version) == ("provider-outage", 1) and "fake-provider" in item.diagnosis["hypothesis"]
    item = grading.saved(REPO / calibration.SAVED_RUNS[0])  # week 3's first live run: no run.json
    assert (item.name, item.version) == ("false-alarm", 4) and item.diagnosis["confidence"] == 0.3
    (tmp_path / "run.json").write_text(json.dumps({"fault": {"name": "x"}, "case": {"name": "false-alarm"}}))
    assert grading.saved(tmp_path) == grading.Saved(tmp_path, "false-alarm", 4, None)  # no trace: the loop never ran
    (tmp_path / "run.json").write_text("[]")
    with pytest.raises(ValueError, match="is not an object"):
        grading.saved(tmp_path)
    with pytest.raises(ValueError, match="holds no run.json"):
        grading.saved(tmp_path / "nothing")


def test_run_folders_walks_a_weeks_runs_and_passes_over_rehearsals(tmp_path):
    week3 = grading.run_folders([REPO / "evidence" / "week3"])
    assert [f.name for f in week3] == [Path(relative).name for relative in calibration.SAVED_RUNS[:4]]
    week4 = grading.run_folders([REPO / "evidence" / "week4", REPO / "evidence" / "week4"])  # named twice: once
    assert len(week4) == 9 and not any("_rehearsal_" in f.name for f in week4)
    suite = tmp_path / "suite" / "a_run"
    suite.mkdir(parents=True)
    (suite / "run.json").write_text("{}")
    assert grading.run_folders([tmp_path]) == [suite]  # two levels down: a week's suites
    assert grading.run_folders([tmp_path / "missing"]) == []


def test_digest_names_a_value_whatever_the_order_of_its_keys():
    assert grading.digest({"a": 1, "b": [2]}) == grading.digest({"b": [2], "a": 1})
    assert grading.digest({"a": 1}) != grading.digest({"a": 2})
    assert len(grading.digest(None)) == 64


# The independent review's own sentences (week 5, item 2): written by a separate Claude, with their expected verdict,
# before it ran them against version 4 as first committed (bec1afb), where several got another verdict (its findings
# M1 to M3 and L1). The fixes were written knowing them, so here they show the fixes work, nothing more.
REVIEW_SENTENCES = [
    (
        "false-alarm",
        "The alert is a false alarm: the ledger consumer group's lag is 0 on every partition and it keeps up with provider-events.",
        "names the cause",
    ),
    (
        "false-alarm",
        "The ledger consumer is healthy and not stuck; its lag is near zero, so this alert is spurious.",
        "names the cause",
    ),
    (
        "false-alarm",
        "The ledger consumer is neither stuck nor stalled: its lag on provider-events reads 0, so the alert is false.",
        "names the cause",
    ),
    (
        "false-alarm",
        "There is no evidence the ledger consumer is stuck; its lag is 0 and the group is caught up, so the alert is false.",
        "names the cause",
    ),
    (
        "false-alarm",
        "The ledger consumer appears to be stalled: lag reads 0 but no offsets were committed recently.",
        "misses the cause",
    ),
    ("false-alarm", "This is not a false alarm: the ledger consumer is really behind.", "misses the cause"),
    (
        "false-alarm",
        "The ledger consumer group is 250 messages behind because the consumer is slow; there is no outage elsewhere.",
        "misses the cause",
    ),
    ("false-alarm", "No real problem with the ledger: lag reads 0, so the alert is wrong.", "names the cause"),
    (
        "false-alarm",
        "The ledger group's lag is 0 and it is up to date; nothing is wrong, and nothing crashed or hung.",
        "names the cause",
    ),
    (
        "false-alarm",
        "The ledger consumer has not crashed and is not hung: lag is 0, so this is a false positive.",
        "names the cause",
    ),
    (
        "false-alarm",
        "The ledger lag is not 0: the alert is right, the ledger consumer is stuck behind a poison message.",
        "misses the cause",
    ),
    (
        "missing-deploys",
        "The ledger consumer stopped deduplicating, so webhooks the provider resends are booked twice. I could not check recent deploys: the deploy list timed out.",
        "names the cause",
    ),
    (
        "missing-deploys",
        "A deploy switched the ledger consumer to LEDGER_HANDLER=naive, so every webhook the provider re-sends is booked again.",
        "partly",
    ),
    (
        "missing-deploys",
        "The ledger consumer did not check idempotency keys, so webhooks the provider resent were booked twice.",
        "partly",
    ),
    (
        "missing-deploys",
        "The ledger consumer's handler failed to check event_id, so each webhook the provider re-sends is booked a second time.",
        "partly",
    ),
    (
        "missing-deploys",
        "The ledger consumer cannot see that an event was already processed, so webhooks sent again are booked twice.",
        "partly",
    ),
    (
        "missing-deploys",
        "Redis evicted the ledger's idempotency keys, so redelivered webhooks are booked twice; I could not verify deploys.",
        "misses the cause",
    ),
    (
        "missing-deploys",
        "The ledger consumer's dedupe is gone, so redelivered webhooks are booked twice. Docker did not answer, so the deploy history is unverified.",
        "names the cause",
    ),
    (
        "duplicate-delivery",
        "The ledger consumer books redelivered webhooks twice because a deploy disabled its idempotency check, rather than Redis losing keys.",
        "names the cause",
    ),
    (
        "duplicate-delivery",
        "Redis did not lose keys: a deploy turned off the ledger consumer's dedupe, so webhooks the provider re-sends are booked twice.",
        "names the cause",
    ),
    (
        "duplicate-delivery",
        "Ruled out: Redis losing its keys. A deploy removed the ledger consumer's dedupe, so webhooks the provider re-sends are booked twice.",
        "names the cause",
    ),
    (
        "duplicate-delivery",
        "Redis dropped the ledger's dedupe keys, so webhooks the provider re-sends are booked twice.",
        "misses the cause",
    ),
    (
        "poison-message",
        "PostgreSQL was not down; one malformed event is retried forever by the ledger consumer.",
        "names the cause",
    ),
    (
        "poison-message",
        "The ledger consumer retries one invalid event forever; the database could be down too.",
        "partly",
    ),
    (
        "poison-message",
        "The ledger consumer retries forever because the database was unavailable and one event failed to book.",
        "misses the cause",
    ),
    (
        "poison-message",
        "The ledger consumer retries forever one event whose amount field is a JSON string, so nothing behind it is booked.",
        "names the cause",
    ),
    (
        "provider-outage",
        "The provider times out; payments-api's circuit breaker tripped, cutting off calls, so payments fail fast with 503.",
        "names the cause",
    ),
    (
        "provider-outage",
        "The provider hangs, so payments-api's circuit breaker opened and it fails fast; without the breaker each payment would wait 9 s.",
        "names the cause",
    ),
    (
        "provider-outage",
        "The provider hangs; the circuit breaker did not open, so each request waited through three attempts.",
        "partly",
    ),
    (
        "provider-outage",
        "The provider is healthy and there is no outage; payments-api's breaker is open because the database is overloaded.",
        "misses the cause",
    ),
    (
        "provider-outage",
        "The database is not slow; the provider is timing out and the breaker is open, so payments fail fast.",
        "names the cause",
    ),
    (
        "provider-outage",
        "PostgreSQL is slow, so payments-api waits on it; the provider and its breaker are fine.",
        "misses the cause",
    ),
    (
        "money-unit",
        "A deploy of payments-api sends amounts in dollars instead of cents, so the provider charges 100 times what our records say.",
        "names the cause",
    ),
    (
        "money-unit",
        "There was no deploy of the payment confirmer; a deploy of payments-api sends each amount 100 times too large, so the provider overcharges.",
        "names the cause",
    ),
    (
        "money-unit",
        "payments-api multiplies each amount by 100 since its deploy; the provider charges 100 times our records. A bug in the ledger consumer was ruled out.",
        "names the cause",
    ),
    (
        "money-unit",
        "A regression in the payment confirmer reads amounts as dollars, so the provider's amounts differ from the payments API's records.",
        "partly",
    ),
]


@pytest.mark.parametrize("fault, hypothesis, expected", REVIEW_SENTENCES)
def test_version_4_gets_the_reviews_own_sentences_right(fault, hypothesis, expected):
    assert verdict(fault, hypothesis, 4) == expected


@pytest.mark.parametrize(
    "text, word, denied",
    [
        ("The ledger consumer is not hung, stalled or crashed.", "crashed", True),  # a negated list, across commas
        ("No sign of a poison message.", "poison", True),
        ("A deploy did it, rather than Redis losing keys.", "Redis", True),
        ("Ruled out: Redis losing its keys.", "Redis", True),  # one or two words before a colon
        ("Redis losing its keys is unlikely.", "Redis", True),  # ruled out after
        ("A bug in the ledger consumer was ruled out.", "ledger", True),
        ("The provider is not answering and PostgreSQL is down.", "PostgreSQL", False),  # a turn after the negation
        ("The provider is not answering, PostgreSQL is down.", "PostgreSQL", False),  # nor past a comma
        ("It could not process events while PostgreSQL was down.", "PostgreSQL", False),
        ("Nothing is wrong: the group is not behind.", "group", False),  # a colon ends a clause of more than two words
        ("The group is not behind (its lag stays near 0).", "lag", False),  # so does a bracket
        ("Not hung. The consumer crashed.", "crashed", False),  # and a full stop
        ("The breaker opened; the database is neither slow nor down.", "database", False),  # the gap stops at neither
    ],
)
def test_a_negation_reaches_through_its_segment_and_a_list_of_the_same_kind_unless_the_sentence_turns(
    text, word, denied
):
    start = text.index(word)
    stuck = grading.RUBRICS_V4["false-alarm"].wrong[0].patterns  # a list of the same kind: "hung, stalled or crashed"
    assert grading._negated(text, start, start + len(word), stuck) is denied


@pytest.mark.parametrize(
    "text, word, hedged",
    [
        ("The ledger consumer retries forever; the database could be down too.", "database could be down", True),
        ("Still open: whether Redis lost its keys.", "Redis", True),  # a hedge before a colon
        ("The ledger retries a bad event, so Redis may have lost keys.", "Redis may have lost", True),  # in it
        ("Redis losing its keys cannot be ruled out.", "Redis", True),  # after it
        ("It could not process events while PostgreSQL was down.", "PostgreSQL", False),  # could not: no hedge
        ("Events could not be booked: PostgreSQL was down.", "PostgreSQL", False),  # nor before a colon
        ("The breaker is open because the database is overloaded.", "database", False),  # open: no hedge here
        ("With the breaker open the database is overloaded.", "database", False),  # nor with no turn after it
        ("Still open: Redis lost its keys.", "Redis", True),  # "open" before a colon is one
        ("Perhaps Redis lost its keys.", "Redis", True),  # earlier in its clause
        ("It may be slow but PostgreSQL was down.", "PostgreSQL", False),  # not past a turn of the sentence
    ],
)
def test_a_wrong_cause_offered_as_a_maybe_is_hedged(text, word, hedged):
    start = text.index(word)
    assert grading._hedged(text, start, start + len(word)) is hedged


def test_a_wrong_cause_only_offered_beside_the_right_one_makes_it_partly_and_never_lifts_a_miss():
    right = "One malformed event is retried forever by the ledger consumer, so nothing behind it is booked."
    assert verdict("poison-message", right, 4) == "names the cause"
    assert verdict("poison-message", f"{right} The database could be down too.", 4) == "partly"
    assert verdict("poison-message", f"{right} The database was down too.", 4) == "misses the cause"
    assert verdict("poison-message", "The ledger is stuck. The database could be down.", 4) == "misses the cause"


@pytest.mark.parametrize(
    "fault, hypothesis, expected",
    [
        # The review's finding L1: the false alarm must not credit a lag that rose, a consumer that fell behind, or one
        # that cannot keep up.
        ("false-alarm", "The ledger consumer group's lag rose from 0 to 250 messages.", "misses the cause"),
        ("false-alarm", "The ledger consumer consumed nothing and fell behind.", "misses the cause"),
        ("false-alarm", "The ledger consumer cannot keep up: its lag climbed past 250.", "misses the cause"),
        # The review's finding L8: "not an integer" is fault 2's message too.
        (
            "poison-message",
            "The ledger consumer retries forever one event whose amount is not an integer.",
            "names the cause",
        ),
        # The re-check's M1: what docker did not answer, in other words.
        (
            "missing-deploys",
            "Docker did not respond to recent_deploys; the ledger consumer's dedupe is gone, so re-sent webhooks are"
            " booked twice.",
            "names the cause",
        ),
        (
            "missing-deploys",
            "The deploy list never answered; the ledger consumer's dedupe is gone, so re-sent webhooks are booked twice.",
            "names the cause",
        ),
        # A database neither slow nor down is no wrong cause.
        (
            "provider-outage",
            "The provider times out and the breaker opened, so payments fail fast; the database is neither slow nor down.",
            "names the cause",
        ),
    ],
)
def test_version_4_reads_the_reviews_other_cases_right(fault, hypothesis, expected):
    assert verdict(fault, hypothesis, 4) == expected


# The re-check's own sentences (week 5, item 2): written by the same separate Claude, with their expected verdict,
# before it ran them against 5c2acbf, where 18 of 36 got another verdict (its findings H1 and M1 to M3: a negation that
# reached across commas let wrong causes pass as named). The fixes were written knowing them: they show the fixes work.
RECHECK_SENTENCES = [
    (
        "false-alarm",
        "A stalled ledger consumer would show its lag climbing; it reads 0 on every partition, so the alert is false.",
        "names the cause",
    ),  # A1
    (
        "false-alarm",
        "If the ledger consumer were stuck, its lag would grow; it is 0, so this is a false alarm.",
        "names the cause",
    ),  # A2
    (
        "false-alarm",
        "I checked for a stuck or crashed ledger consumer and found none: lag is 0, so the alert is false.",
        "names the cause",
    ),  # A3
    ("false-alarm", "The ledger consumer is not behind, the alert is false.", "names the cause"),  # A4
    (
        "false-alarm",
        "Checked the ledger group: no stall, no crash, lag 0. The alert is false.",
        "names the cause",
    ),  # A5
    (
        "false-alarm",
        "This is not a false alarm, the ledger consumer is stuck behind one bad event.",
        "misses the cause",
    ),  # A6
    (
        "false-alarm",
        "The ledger group's lag is not near 0, it keeps growing, so the alert is right.",
        "misses the cause",
    ),  # A7
    (
        "false-alarm",
        "The alert is probably false: lag reads 0, though the ledger consumer may be stalled.",
        "partly",
    ),  # A9
    (
        "false-alarm",
        "Nothing is wrong with the ledger consumer, but the alert is false: its lag is 0.",
        "names the cause",
    ),  # A10
    (
        "false-alarm",
        "While the alert says 250 behind, the ledger group reads 0 lag and is caught up; the alert is false.",
        "names the cause",
    ),  # A11
    (
        "duplicate-delivery",
        "A deploy turned off the ledger consumer's dedupe, so webhooks the provider re-sends are booked twice; Redis eviction is unlikely, since every key is still there.",
        "names the cause",
    ),  # B1
    (
        "duplicate-delivery",
        "The handler did not change, Redis evicted the ledger consumer's idempotency keys, so webhooks the provider re-sends are booked twice.",
        "misses the cause",
    ),  # B3
    (
        "duplicate-delivery",
        "No deploy touched the ledger consumer, Redis lost its dedupe keys, and the provider's re-sent webhooks were booked twice.",
        "misses the cause",
    ),  # B4
    (
        "duplicate-delivery",
        "Not a code change: Redis flushed the idempotency keys, so the ledger consumer books re-sent webhooks twice.",
        "misses the cause",
    ),  # B5
    (
        "duplicate-delivery",
        "A deploy switched the ledger consumer to a handler without dedupe (Redis lost nothing), so each webhook the provider re-sends is booked twice.",
        "names the cause",
    ),  # B7
    (
        "duplicate-delivery",
        "While Redis held every key, the ledger consumer's new handler skipped its dedupe check, so re-sent webhooks were booked twice.",
        "names the cause",
    ),  # B8
    (
        "poison-message",
        "The event is not malformed, PostgreSQL was down, and the ledger consumer retries every message forever.",
        "misses the cause",
    ),  # C1
    (
        "poison-message",
        "One malformed event is retried forever by the ledger consumer; a database outage is ruled out, since PostgreSQL answered every query.",
        "names the cause",
    ),  # C2
    (
        "poison-message",
        "The ledger consumer retries one malformed event forever; the database was up, not down.",
        "names the cause",
    ),  # C3
    (
        "poison-message",
        "The ledger consumer retries one malformed event forever, or PostgreSQL was briefly unavailable.",
        "partly",
    ),  # C4
    (
        "poison-message",
        "While PostgreSQL was down, the ledger consumer failed to book events and is retrying them.",
        "misses the cause",
    ),  # C5
    (
        "provider-outage",
        "The provider hangs and the circuit breaker has not opened, so every payment waits through three attempts.",
        "partly",
    ),  # D1
    (
        "provider-outage",
        "The provider times out, and payments-api's circuit breaker is still closed, so requests wait 9 s.",
        "partly",
    ),  # D2
    (
        "provider-outage",
        "The provider stopped answering; payments-api's circuit breaker tripped and cuts off calls to it, so payments fail fast with 503.",
        "names the cause",
    ),  # D3
    (
        "provider-outage",
        "The provider hangs and payments-api has no circuit breaker, so requests could pile up for 9 s.",
        "partly",
    ),  # D4
    (
        "provider-outage",
        "The provider is not down, the database is overloaded and payments-api waits on it, so its breaker opened.",
        "misses the cause",
    ),  # D5
    (
        "provider-outage",
        "Ruled out: a slow database. The provider hangs and the breaker is open, so payments-api fails fast.",
        "names the cause",
    ),  # D6
    (
        "money-unit",
        "A deploy of payments-api reads our amounts as dollars (not a bug in the payment confirmer), so the provider charges 100 times what we record.",
        "names the cause",
    ),  # E1
    (
        "money-unit",
        "The payment confirmer has no bug; payments-api sends each amount in dollars since its deploy, so the provider charges 100x.",
        "names the cause",
    ),  # E2
    (
        "money-unit",
        "The payment confirmer reads the provider's amounts as dollars, so our records differ from the provider's charges.",
        "misses the cause",
    ),  # E3
    (
        "missing-deploys",
        "Since the tools did not answer, I could not check the deploy list; the ledger consumer's dedupe is gone, so re-sent webhooks are booked twice.",
        "names the cause",
    ),  # F1
    (
        "missing-deploys",
        "Neither recent_deploys nor service_health answered, docker timed out; the ledger consumer's dedupe is gone, so every webhook the provider re-sends is booked twice.",
        "names the cause",
    ),  # F2
    (
        "missing-deploys",
        "A deploy switched the ledger consumer to LEDGER_HANDLER=naive, so re-sent webhooks are booked twice; the reversal plan is unverified.",
        "partly",
    ),  # F3
    (
        "missing-deploys",
        "I could not read recent deploys or service health (docker timed out); the ledger consumer double-books re-sent webhooks because its dedupe is gone.",
        "names the cause",
    ),  # F6
    (
        "missing-deploys",
        "The deploy list answered normally and shows no deploy; the ledger consumer's dedupe is gone, so re-sent webhooks are booked twice.",
        "partly",
    ),  # F8
]


@pytest.mark.parametrize("fault, hypothesis, expected", RECHECK_SENTENCES)
def test_version_4_gets_the_rechecks_own_sentences_right(fault, hypothesis, expected):
    assert verdict(fault, hypothesis, 4) == expected


# Version 4's own (the re-check's sentence B6): a wrong cause offered only as a maybe, and no other cause. A person says
# it misses; the rule reads the maybe as offered beside a right cause, whose words here are the wrong cause's own.
KNOWN_LIMITS_V4 = [
    (
        "duplicate-delivery",
        "Redis may have evicted the ledger consumer's idempotency keys, so webhooks the provider re-sends are booked twice.",
        "misses the cause",
    ),  # B6
]


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="a known limit of rubric version 4: it reads words, not meaning"
)
@pytest.mark.parametrize("fault, hypothesis, right", KNOWN_LIMITS_V4)
def test_a_known_limit_of_version_4(fault, hypothesis, right):
    assert verdict(fault, hypothesis, version=4) == right


@pytest.mark.parametrize(
    "text, wrong_cause",
    [
        # The re-check's H1: a negation about something else, before a comma, does not deny the wrong cause after it.
        ("The handler did not change, Redis evicted the keys, so webhooks are booked twice.", "Redis lost the keys"),
        ("The event is not malformed, PostgreSQL was down, and the ledger retries forever.", "the database was down"),
        ("PostgreSQL was down, so a malformed event is unlikely.", "the database was down"),  # unlikely: the event's
    ],
)
def test_a_negation_or_a_ruling_out_in_another_segment_does_not_deny_a_wrong_cause(text, wrong_cause):
    rubric = next(r for r in grading.RUBRICS_V4.values() if any(w.name == wrong_cause for w in r.wrong))
    element = next(w for w in rubric.wrong if w.name == wrong_cause)
    assert grading._stance(element, text) == "asserted"
