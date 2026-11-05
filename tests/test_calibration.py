"""The graders checked (klaxon.calibration), offline: the 66 sentences and the 13 saved diagnoses; the rule's verdict on
each, worked out again; the judge on each sentence, one call each; the report of each grader against the expected
verdicts and Aravind's; the replay; the command line.

Every judge reply here is scripted (ScriptedModel), or scripted and recorded as a live reply would be (RecordedScript),
and labeled as made up: no model is called. Aravind's verdicts here are written by the tests, standing in for his: a
test never writes into the real evidence."""

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from klaxon import calibration, grading, judge, labels
from klaxon.agent import claude_code, gate
from klaxon.agent.model import ScriptedModel, read_records
from klaxon.patient.stack import REPO
from tests.agent.test_postmortem import RecordedScript


def reply(verdict="names the cause", fix_fits=None, why="Made up for the test."):
    return json.dumps({"verdict": verdict, "fix_fits": fix_fits, "why": why})


# The two sets


def test_the_66_sentences_come_from_the_grading_tests_each_with_its_expected_verdict_and_system():
    assert len(calibration.SENTENCES) == 66
    assert Counter(s.source for s in calibration.SENTENCES) == {
        calibration.V1_TESTS: 9,
        calibration.V2_TESTS: 6,
        calibration.V3_TESTS: 42,
        calibration.V1_LIMIT: 7,
        calibration.V2_LIMIT: 1,
        calibration.V3_LIMIT: 1,
    }
    assert len({(s.name, s.system, s.hypothesis) for s in calibration.SENTENCES}) == 66
    for s in calibration.SENTENCES:
        assert s.expected in judge.VERDICTS and s.name in grading.VERSIONS[s.system]


def test_the_13_saved_diagnoses_are_the_12_fault_runs_and_week_3s_first_live_run():
    items = [grading.saved(REPO / relative) for relative in calibration.SAVED_RUNS]
    assert all(item.diagnosis is not None for item in items)
    assert Counter(item.name for item in items) == {
        "false-alarm": 1,
        "duplicate-delivery": 2,
        "poison-message": 3,
        "provider-outage": 2,
        "money-unit": 2,
        "secret-rotation": 2,
        "pool-exhaustion": 1,
    }
    assert grading.run_folders([REPO / "evidence" / "week3", REPO / "evidence" / "week4"]) == [
        REPO / relative for relative in calibration.SAVED_RUNS
    ]


def test_the_rule_agrees_with_64_of_the_66_and_misses_only_where_version_1_grades_fault_3_before_the_breaker():
    # Measured offline: a regression check on the rule, not a claim that it reads meaning (its version 4 patterns were
    # written knowing these sentences).
    graded = [(s, calibration.rule_on(s)) for s in calibration.SENTENCES]
    assert Counter(rule["rubric_version"] for _, rule in graded) == {4: 61, 1: 5}
    wrong = [(s.hypothesis, rule["verdict"]) for s, rule in graded if rule["verdict"] != s.expected]
    assert wrong == [
        ("PostgreSQL is slow and payments-api requests wait on it; fake-provider looks healthy.", "names the cause"),
        ("payments-api is slow because a traffic spike exhausted its worker threads.", "partly"),
    ]


# The judge on the sentences


def test_the_judge_gets_each_sentence_alone_with_the_right_answer_of_its_system_one_call_each():
    sentences = [calibration.SENTENCES[0], next(s for s in calibration.SENTENCES if s.name == "provider-outage")]
    model = ScriptedModel([reply(), reply("partly")])
    seen = []
    found = calibration.judge_sentences(model, sentences, lambda done: seen.append(len(done)))
    assert seen == [1, 2] and len(model.calls) == 2
    for (_, turns), sentence in zip(model.calls, sentences, strict=True):
        right = grading.answer(sentence.name, sentence.system)
        assert turns[0].content == judge.first_turn(judge.document(right, {"hypothesis": sentence.hypothesis}))
    assert "has no circuit breaker" in json.loads(model.calls[1][1][0].content.split("\n")[2])["case"]["right_answer"]
    assert [x["judgment"]["verdict"] for x in found] == ["names the cause", "partly"]
    assert found[0] | {"judgment": None} == {
        "sentence": 1,
        "name": sentences[0].name,
        "system": sentences[0].system,
        "hypothesis": sentences[0].hypothesis,
        "expected": sentences[0].expected,
        "source": sentences[0].source,
        "judgment": None,
    }


# The report, from saved files


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The 13 saved runs copied to a made-up repo, and the calibration's evidence folder there: nothing is written into
    the real evidence."""
    for relative in calibration.SAVED_RUNS:
        shutil.copytree(REPO / relative, tmp_path / relative)
    monkeypatch.setattr(calibration, "REPO", tmp_path)
    monkeypatch.setattr(calibration, "EVIDENCE", tmp_path / "evidence" / "week5")
    return tmp_path


def calibrated(world, verdicts):
    """A calibration folder whose sentences the judge gave these verdicts (scripted, made up)."""
    folder = world / "evidence" / "week5" / "2026-11-05T000000Z_calibration"
    folder.mkdir(parents=True)
    model = ScriptedModel([reply(v) for v in verdicts])
    found = calibration.judge_sentences(model, calibration.SENTENCES[: len(verdicts)])
    calibration._write(folder, found)
    return folder


def test_the_report_counts_each_grader_against_the_expected_verdicts(world):
    first = calibration.SENTENCES[:3]  # names the cause, partly, misses the cause
    folder = calibrated(world, ["names the cause", "names the cause", "unsure"])
    found = calibration.report(folder)
    said = found["agreement"]["sentences"]
    assert [s.expected for s in first] == list(judge.VERDICTS)
    assert said["count"] == 3 and said["rule_with_expected"] == {"agree": 3, "of": 3}
    assert said["judge_with_expected"] == {"agree": 1, "of": 2} and said["judge_no_verdict"] == 1
    assert said["judge_matrix"]["partly"]["names the cause"] == 1
    assert said["judge_matrix"]["misses the cause"][judge.NO_VERDICT] == 1
    page = calibration.markdown(found)
    assert "- The rule agrees with the expected verdict on 3 of 3." in page
    assert "- The judge agrees on 1 of 2; no verdict on 1; judged 3 of 3." in page
    assert "expected partly; rule partly (version 4); judge names the cause; no fix given: Made up" in page


def test_the_report_puts_the_rule_the_judge_and_aravind_side_by_side_on_the_saved_runs(world):
    item_folder = world / calibration.SAVED_RUNS[0]  # week 3's first live run
    item = grading.saved(item_folder)
    judged = judge.judge(
        ScriptedModel([reply("misses the cause", False)]), grading.answer("false-alarm"), item.diagnosis
    )
    judge.save(item_folder, item, judged)
    mine = {"verdict": "misses the cause", "fix_fits": False, "why": None, "how": gate.TYPED}
    mine |= {"diagnosis_sha256": grading.digest(item.diagnosis)}
    (item_folder / labels.LABEL_FILE).write_text(json.dumps(mine))  # standing in for his, typed at a terminal
    other = world / calibration.SAVED_RUNS[1]
    (other / labels.LABEL_FILE).write_text(json.dumps(mine | {"verdict": "partly", "diagnosis_sha256": "x"}))
    third = world / calibration.SAVED_RUNS[2]
    (third / judge.SAVED).write_text(json.dumps({"diagnosis_sha256": "x", "judgment": {"verdict": "partly"}}))
    found = calibration.report(None)
    said = found["agreement"]["runs"]
    assert (said["count"], said["judged"], said["labeled"]) == (13, 1, 1)  # one of each graded another diagnosis
    assert found["runs"][2]["problems"] == [
        "the judge graded another diagnosis than the run's evidence holds now: not counted"
    ]
    assert said["rule_with_judge"] == {"agree": 1, "of": 1} and said["judge_with_aravind"] == {"agree": 1, "of": 1}
    assert said["rule_with_aravind"] == {"agree": 1, "of": 1} and said["rule_fix_with_aravind"] == {"agree": 1, "of": 1}
    assert found["runs"][1]["problems"] == [
        "Aravind's verdict is on another diagnosis than the run's evidence holds now: not counted"
    ]
    assert found["runs"][1]["aravind"] is None and found["runs"][2]["judge"] is None
    page = calibration.markdown(found)
    assert "| 2026-10-18T112951Z_loop_ledgerlaghigh | false-alarm | misses the cause (v4), fix False |" in page
    assert (
        "| 2026-10-19T024001Z_fault-run_provider-outage | provider-outage | names the cause (v1), fix False | - | - |"
        in page
    )
    assert "Judged: 1 of 13. Labeled by Aravind: 1 of 13." in page and "Problems:" in page


def test_before_any_judge_call_the_report_holds_the_66_sentences_and_the_rules_verdicts(world):
    found = calibration.report(None)
    said = found["agreement"]["sentences"]
    assert (said["count"], said["judged"], said["rule_with_expected"]) == (66, 0, {"agree": 64, "of": 66})
    assert said["judge_with_expected"] == {"agree": 0, "of": 0} and sum(said["judge_matrix"]["partly"].values()) == 0
    page = calibration.markdown(found)
    assert "- The judge agrees on -; no verdict on 0; judged 0 of 66." in page
    assert page.count("judge not judged yet") == 2  # the two the rule gets wrong, both by version 1


def test_latest_is_the_newest_calibration_folder(world):
    assert calibration.latest() is None
    older = calibrated(world, ["partly"])
    newer = older.with_name("2026-11-06T000000Z_calibration")
    shutil.copytree(older, newer)
    assert calibration.latest() == newer


# Replay, and the command line, with the live adapter a RecordedScript


@pytest.fixture
def calibrating(world, monkeypatch):
    """The calibration's commands on two sentences and two saved runs, with Claude Code found at a made-up path and the
    live adapter a RecordedScript."""
    monkeypatch.setattr(calibration, "SENTENCES", calibration.SENTENCES[:2])
    monkeypatch.setattr(calibration, "SAVED_RUNS", calibration.SAVED_RUNS[:2])
    monkeypatch.setattr(calibration, "find_live_model", lambda model: Path("/nonexistent/claude"))
    monkeypatch.setattr(judge, "find_live_model", lambda model: Path("/nonexistent/claude"))
    monkeypatch.setattr(claude_code, "ClaudeCode", RecordedScript)

    def run(*script):
        RecordedScript.script = list(script)
        code = calibration.main(["run"])
        assert RecordedScript.script == []  # every scripted reply was asked for
        return code

    return run


def test_the_run_command_judges_the_sentences_then_the_saved_runs_and_replays(world, calibrating, capsys):
    script = [reply(), reply("partly"), reply("misses the cause", False), reply("names the cause", True)]
    assert calibrating(*script) == 0
    out = capsys.readouterr().out
    assert (
        "Sentences judged: 2; no verdict: 0." in out and "Saved runs judged: 2; no verdict: 0; passed over: 0." in out
    )
    folder = calibration.latest()
    saved = json.loads((folder / calibration.CALIBRATION).read_text())
    assert saved["label"] == calibration.LABEL and len(saved["sentences"]) == 2
    assert saved["judge"] == {"model": "claude-opus-5-5", "version": 1, "sha256": judge.fingerprint()}
    records = read_records(folder / calibration.RECORDING)
    assert records[0]["what"] == calibration.WHAT and [r["kind"] for r in records].count("response") == 2
    for relative in calibration.SAVED_RUNS:
        assert judge.read(world / relative)["judgment"]["verdict"] in judge.VERDICTS
    assert calibration.main(["replay"]) == 0
    assert capsys.readouterr().out.startswith("Reproduced: the same prompts, replies and judgments as recorded")
    assert calibration.main(["report", "--write"]) == 0
    assert (folder / "calibration-report.md").read_text().startswith("# Klaxon's graders, checked")


def test_a_replay_finds_a_changed_judgment(world, calibrating, capsys):
    assert calibrating(reply(), reply("partly"), reply("misses the cause", False), reply("names the cause", True)) == 0
    path = calibration.latest() / calibration.CALIBRATION
    saved = json.loads(path.read_text())
    saved["sentences"][1]["judgment"]["verdict"] = "names the cause"
    path.write_text(json.dumps(saved))
    assert calibration.replay(calibration.latest()) == ["sentence 2: the judgment differs"]


def test_a_replay_finds_a_recorded_call_it_did_not_ask_for_again(world, calibrating):
    assert calibrating(reply(), reply("partly"), reply("misses the cause", False), reply("names the cause", True)) == 0
    recording = calibration.latest() / calibration.RECORDING
    response = next(line for line in recording.read_text().splitlines() if '"kind":"response"' in line)
    with open(recording, "a") as out:
        out.write(response + "\n")
    assert calibration.replay(calibration.latest()) == ["1 recorded call(s) were not asked for again"]


def test_a_sentence_with_no_verdict_exits_1(world, calibrating):
    assert calibrating(reply("unsure"), reply(), reply("misses the cause", False), reply("names the cause", True)) == 1


def test_the_run_command_without_claude_code_starts_nothing_and_writes_nothing(world, capsys):
    assert calibration.main(["run"]) == 2  # the tests find no Claude Code (conftest)
    assert "Not started, and nothing was written" in capsys.readouterr().err
    assert not (world / "evidence" / "week5").exists()


def test_the_report_and_replay_commands_with_no_calibration_folder(world, capsys):
    assert calibration.main(["report"]) == 0
    assert capsys.readouterr().out.startswith("No calibration folder yet: the saved runs only.")
    assert calibration.main(["replay"]) == 2
    assert "no calibration folder yet in evidence/week5" in capsys.readouterr().err
    assert calibration.main(["report", str(world)]) == 2
    assert "holds no calibration.json" in capsys.readouterr().err
