"""The rule that compares a diagnosis with the cause a fault records. The hypotheses here are written for the tests,
except one: the first live run's, a recorded response from Claude (claude-opus-5-5, 2026-10-18), read from its
evidence as data."""

import json
import re

import pytest

from klaxon import alerts, faults, grading
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


@pytest.mark.parametrize("version", VERSIONS)
def test_every_fault_has_a_rubric_with_three_required_elements(version):
    assert set(grading.VERSIONS[version]) == set(faults.FAULTS)
    for rubric in grading.VERSIONS[version].values():
        assert sum(element.required for element in rubric.elements) == 3
        for element in rubric.elements:
            for pattern in element.patterns:
                re.compile(pattern)


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("name", sorted(faults.FAULTS))
def test_each_faults_own_recorded_cause_names_the_cause(name, version):
    found = grading.compare(name, diagnosis(faults.FAULTS[name].recorded(version).cause), version)
    assert found["verdict"] == "names the cause"
    assert all(item["named"] for item in found["elements"])


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("name, other", [(a, b) for a in faults.FAULTS for b in faults.FAULTS if a != b])
def test_no_faults_cause_names_another_faults_cause(name, other, version):
    assert verdict(other, faults.FAULTS[name].recorded(version).cause, version) != "names the cause"


@pytest.mark.parametrize("version", VERSIONS)
def test_the_first_live_runs_wrong_diagnosis_misses_every_faults_cause(version):
    # It called an idle ledger consumer "hung or silently stalled": that restates the alert, and names no cause.
    recorded = read_trace(FIRST_LIVE_RUN)[-1]["diagnosis"]
    for name in faults.FAULTS:
        assert grading.compare(name, recorded, version)["verdict"] == "misses the cause", name


@pytest.mark.parametrize(
    "fault, hypothesis, expected",
    [
        (
            "duplicate-delivery",
            "A redeploy of the ledger consumer switched it to a handler without dedupe, so every webhook the provider"
            " redelivers after a lost ack is booked a second time.",
            "names the cause",
        ),
        (
            "duplicate-delivery",
            "The ledger consumer no longer checks idempotency keys, so some events are posted twice.",
            "partly",
        ),
        ("duplicate-delivery", "The ledger is posting some events twice.", "misses the cause"),
        (
            "poison-message",
            "One malformed payment.updated event is retried forever by the ledger consumer (BAD_EVENTS=retry after a"
            " deploy), so nothing behind it is booked.",
            "names the cause",
        ),
        ("poison-message", "The ledger consumer keeps retrying and is stuck.", "partly"),
        ("poison-message", "The ledger consumer has stalled and its lag grows.", "misses the cause"),
        (
            "provider-outage",
            "The payment provider is timing out, and with no circuit breaker each payment waits through three attempts.",
            "names the cause",
        ),
        ("provider-outage", "The provider is down.", "partly"),
        ("provider-outage", "The database connection pool is exhausted.", "misses the cause"),
    ],
)
def test_hypotheses_written_for_the_test_get_the_expected_verdict(fault, hypothesis, expected):
    assert verdict(fault, hypothesis, version=1) == expected
    if fault != "provider-outage":  # version 2 changed only fault 3
        assert verdict(fault, hypothesis, version=2) == expected


@pytest.mark.parametrize(
    "hypothesis, expected",
    [
        (
            "The payment provider hangs: our calls to it time out, the circuit breaker opened, and payments-api now"
            " fails fast with 503.",
            "names the cause",
        ),
        ("The provider is unavailable, and our breaker is open.", "names the cause"),
        ("The provider is down.", "partly"),
        ("payments-api short-circuits every payment with a 503.", "partly"),
        ("The database connection pool is exhausted.", "misses the cause"),
        # Version 1's right answer, before the breaker: in version 2's system, it misses how the API fails.
        ("The provider times out and payments-api waits through three attempts, its workers tied up.", "partly"),
    ],
)
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


def test_the_rubric_is_version_1_exactly_as_committed_before_the_live_runs():
    # Finding 2. Any change to an element, a pattern or a fitting fix changes this fingerprint. Then: a new version,
    # its reason in DECISIONS.md, and the verdicts already recorded kept as they were.
    assert grading.fingerprint(1) == "81fd17fd69a6c0cf5086e59455babf2d736b9cedb7c9191c9ce68894f3323dfb"
    assert grading.compare("poison-message", diagnosis("x"), version=1)["rubric"] == {
        "version": 1,
        "sha256": grading.fingerprint(1),
    }
    assert "rubric version 1, committed in 95a64ab before the first live runs on the faults" in grading.BY[1]


def test_version_2_is_version_1_but_for_fault_3_and_new_comparisons_use_it():
    # Week 4, item 3: committed before any live run on fault 3 with the circuit breaker. Pinned the same way.
    assert grading.RUBRIC_VERSION == 2 and grading.RUBRICS is grading.VERSIONS[2]
    assert grading.fingerprint() == grading.fingerprint(2)
    assert grading.fingerprint(2) == "3aafea2a2d77b934fada2e8817d8e693e23e1a895e9eeacea41cd366a34356c9"
    assert {name: rubric for name, rubric in grading.VERSIONS[2].items() if name != "provider-outage"} == {
        name: rubric for name, rubric in grading.VERSIONS[1].items() if name != "provider-outage"
    }
    assert grading.VERSIONS[2]["provider-outage"] != grading.VERSIONS[1]["provider-outage"]
    found = grading.compare("provider-outage", diagnosis("x"))
    assert found["rubric"] == {"version": 2, "sha256": grading.fingerprint(2)}
    assert (found["recorded_cause"], found["remedy"]) == (
        faults.FAULTS["provider-outage"].cause,
        faults.FAULTS["provider-outage"].remedy,
    )
    assert "rubric version 2, committed before any live run on fault 3 with the circuit breaker" in grading.BY[2]


def test_a_comparison_by_version_1_reads_the_cause_fault_3_recorded_then():
    found = grading.compare("provider-outage", diagnosis("x"), version=1)
    then = faults.FAULTS["provider-outage"].recorded(1)
    assert (found["recorded_cause"], found["remedy"]) == (then.cause, then.remedy)
    assert "has no circuit breaker" in then.cause and "circuit breaker opens" in faults.FAULTS["provider-outage"].cause
    assert faults.FAULTS["duplicate-delivery"].recorded(1).cause == faults.FAULTS["duplicate-delivery"].cause


@pytest.mark.parametrize(
    "saved, version",
    [
        (None, 2),  # no comparison saved: the latest
        ({"verdict": "names the cause"}, 1),  # saved before comparisons recorded a version: the week 3 runs
        ({"rubric": {"version": 1, "sha256": "x"}}, 1),
        ({"rubric": {"version": 2, "sha256": "x"}}, 2),
    ],
)
def test_a_saved_comparison_is_graded_again_with_its_own_version(saved, version):
    assert grading.version_of(saved) == version


@pytest.mark.parametrize("rubric", [{"version": 9}, {"version": "1"}, "1", {}])
def test_a_saved_comparison_naming_a_version_this_code_does_not_have_is_refused(rubric):
    with pytest.raises(ValueError, match="does not have"):
        grading.version_of({"rubric": rubric})


@pytest.mark.parametrize("version", VERSIONS)
def test_an_alerts_own_words_never_name_enough_of_its_faults_cause(version):
    # Finding 2: restating the alert is not a diagnosis. The alerts the live runs raised (recorded in their run.json),
    # each read as a hypothesis, miss their fault's cause; so do the rules' sentences for those symptoms.
    saved = [json.loads(path.read_text()) for path in sorted(FAULT_RUNS.glob("*_fault-run_*/run.json"))]
    recorded = [(run["fault"]["name"], run["alert"]["summary"]) for run in saved if run["alert"] is not None]
    assert {fault for fault, _ in recorded} == set(faults.FAULTS)  # a run that stopped before its alert has none
    made = {
        "duplicate-delivery": alerts.money(
            {"outcome": "failed", "invariants": 5, "failed": {"posted_exactly_once": 7}, "money_off_cents": 1}
        ).sentence,
        "poison-message": alerts.lags([({"consumer": "ledger"}, 271.0)])[0].sentence,
        "provider-outage": f"{alerts.errors(2.2, 2.97).sentence} {alerts.latency(10.0).sentence}",
    }
    for fault, sentence in [*recorded, *made.items()]:
        assert verdict(fault, sentence, version) == "misses the cause", (fault, sentence)


# Finding 2's examples: what a person would say, and version 1 does not. Each is a known limit, kept as a test that
# fails today (strict: one that starts to pass fails too), for the week 5 grader to do better on.
KNOWN_LIMITS = [
    (
        "poison-message",
        "The ledger consumer could not process events while PostgreSQL was down; it is retrying.",
        "misses the cause",
    ),
    (
        "duplicate-delivery",
        "Redis lost its idempotency keys, so the ledger books webhooks the provider re-sends a second time.",
        "misses the cause",
    ),
    (
        "provider-outage",
        "PostgreSQL is slow and payments-api requests wait on it; fake-provider looks healthy.",
        "misses the cause",
    ),
    (
        "provider-outage",
        "payments-api is slow because a traffic spike exhausted its worker threads.",
        "misses the cause",
    ),
    (
        "poison-message",
        "The ledger consumer retries forever one payment.updated event whose amount is not positive integer cents.",
        "names the cause",
    ),
    ("poison-message", "The ledger consumer retries forever one event whose amount is a string.", "names the cause"),
    (
        "duplicate-delivery",
        "A deploy turned off the ledger consumer's duplicate check, so webhooks the provider sends again are booked twice.",
        "names the cause",
    ),
]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a known limit of rubric version 1 (finding 2): it reads words, not meaning",
)
@pytest.mark.parametrize("fault, hypothesis, right", KNOWN_LIMITS)
def test_a_known_limit_of_version_1(fault, hypothesis, right):
    assert verdict(fault, hypothesis, version=1) == right


# Version 2's own: in a system with the breaker, a hypothesis that says there is none names the word, and is wrong.
KNOWN_LIMITS_V2 = [
    (
        "The provider times out, and payments-api has no circuit breaker, so every payment waits through three tries.",
        "partly",
    ),
]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a known limit of rubric version 2: it reads words, not meaning",
)
@pytest.mark.parametrize("hypothesis, right", KNOWN_LIMITS_V2)
def test_a_known_limit_of_version_2(hypothesis, right):
    assert verdict("provider-outage", hypothesis, version=2) == right
