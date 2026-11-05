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
from klaxon.agent.model import ModelTimeout, ScriptedModel, read_records
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
    found, stopped = calibration.judge_sentences(model, sentences, lambda done, why: seen.append((len(done), why)))
    assert seen == [(1, None), (2, None)] and len(model.calls) == 2 and stopped is None
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


def test_the_judge_on_the_sentences_stops_at_the_first_call_with_no_reply():
    # The review's finding M4: a seat's limit stops the calibration, and what was judged is kept.
    model = ScriptedModel([reply(), ModelTimeout("no reply within 300 s"), reply()])
    seen = []
    found, stopped = calibration.judge_sentences(model, calibration.SENTENCES[:3], lambda d, why: seen.append(why))
    assert len(found) == 1 and len(model.calls) == 2
    assert stopped == "at sentence 2: the call gave no reply Klaxon could use (ModelTimeout)" and seen[-1] == stopped


# The report, from saved files


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The 13 saved runs copied to a made-up repo as they were before any grader (no judgment, no verdict of his: the
    review's finding H1), and the calibration's evidence folder there: nothing is written into the real evidence."""
    ignore = shutil.ignore_patterns(judge.SAVED, judge.RECORDING, labels.LABEL_FILE)
    for relative in calibration.SAVED_RUNS:
        shutil.copytree(REPO / relative, tmp_path / relative, ignore=ignore)
    monkeypatch.setattr(calibration, "REPO", tmp_path)
    monkeypatch.setattr(calibration, "EVIDENCE", tmp_path / "evidence" / "week5")
    return tmp_path


def calibrated(world, verdicts, stopped=None):
    """A calibration folder whose first sentences the judge gave these verdicts (scripted, made up)."""
    folder = world / "evidence" / "week5" / "2026-11-05T000000Z_calibration"
    folder.mkdir(parents=True)
    model = ScriptedModel([reply(v) for v in verdicts])
    found, _ = calibration.judge_sentences(model, calibration.SENTENCES[: len(verdicts)])
    calibration._write(folder, found, stopped)
    return folder


def his(folder, verdict, fix_fits, **changes):
    """A verdict of Aravind's on a saved run, written by the test (standing in for his, typed at a terminal)."""
    item = grading.saved(folder)
    right = grading.answer(item.name, item.version)
    mine = {"verdict": verdict, "fix_fits": fix_fits, "why": None, "how": gate.TYPED}
    mine |= {
        "diagnosis_sha256": grading.digest(item.diagnosis),
        "right_answer_sha256": grading.digest([right.cause, right.remedy]),
    }
    (folder / labels.LABEL_FILE).write_text(json.dumps(mine | changes))


def test_the_report_counts_each_grader_against_the_expected_verdicts_on_all_66(world):
    first = calibration.SENTENCES[:3]  # names the cause, partly, misses the cause
    folder = calibrated(world, ["names the cause", "names the cause", "unsure"])
    found = calibration.report(folder)
    said = found["agreement"]["sentences"]
    assert [s.expected for s in first] == list(judge.VERDICTS)
    assert (said["count"], said["judged"]) == (66, 3) and said["rule_with_expected"] == {"agree": 64, "of": 66}
    assert said["judge_with_expected"] == {"agree": 1, "of": 2} and said["judge_no_verdict"] == 1
    assert said["judge_matrix"]["partly"]["names the cause"] == 1
    assert said["judge_matrix"]["misses the cause"][judge.NO_VERDICT] == 1
    page = calibration.markdown(found)
    assert "- The rule agrees with the expected verdict on 64 of 66." in page
    assert "- The judge agrees on 1 of 2; no verdict on 1; judged 3 of 66." in page  # the review's M5: never "3 of 3"
    assert "expected partly; rule partly (version 4); judge names the cause; no fix given: Made up" in page


def test_a_calibration_that_stopped_says_so_and_what_it_did_not_judge(world):
    folder = calibrated(world, ["names the cause"], stopped="at sentence 2: the call gave no reply (ModelTimeout)")
    page = calibration.markdown(calibration.report(folder))
    assert "judged 1 of 66." in page
    assert (
        "- The calibration stopped at sentence 2: the call gave no reply (ModelTimeout): the sentences after it" in page
    )


def test_a_calibration_by_another_version_of_the_judge_counts_none_of_its_judgments(world):
    # The re-check's finding L4: a judge changed since is a new version, checked again, never this one.
    folder = calibrated(world, ["names the cause", "partly"])
    _edit(folder, calibration.CALIBRATION, lambda saved: saved["judge"].update(sha256="an older judge"))
    found = calibration.report(folder)
    assert found["agreement"]["sentences"]["judged"] == 0
    assert found["problems"] == ["its judgments were made by another version of the judge: none is counted"]


def test_a_judged_sentence_the_tests_no_longer_hold_is_said(world):
    folder = calibrated(world, ["names the cause"])
    path = folder / calibration.CALIBRATION
    saved = json.loads(path.read_text())
    saved["sentences"][0]["hypothesis"] = "A sentence since changed."
    path.write_text(json.dumps(saved))
    found = calibration.report(folder)
    assert found["agreement"]["sentences"]["judged"] == 0
    assert found["problems"] == ["a judged sentence the tests no longer hold: A sentence since changed."]
    assert "- the calibration: a judged sentence the tests no longer hold" in calibration.markdown(found)


def test_the_report_puts_the_rule_the_judge_and_aravind_side_by_side_and_holds_back_unlabeled_runs(world):
    first = world / calibration.SAVED_RUNS[0]  # week 3's first live run
    item = grading.saved(first)
    judged = judge.judge(
        ScriptedModel([reply("misses the cause", False)]), grading.answer("false-alarm"), item.diagnosis
    )
    judge.save(first, item, judged)
    his(first, "misses the cause", False)
    second = world / calibration.SAVED_RUNS[1]
    his(second, "partly", False, diagnosis_sha256="x")  # on another diagnosis: not counted
    third = world / calibration.SAVED_RUNS[2]
    other = judge.judge(ScriptedModel([reply("partly", True)]), grading.answer("poison-message"), {"hypothesis": "x"})
    judge.save(third, grading.saved(third), other)
    _edit(third, judge.SAVED, lambda saved: saved.update(diagnosis_sha256="x"))
    fourth = world / calibration.SAVED_RUNS[3]
    unlabeled = judge.judge(
        ScriptedModel([reply("partly", True)]), grading.answer("provider-outage", 1), {"hypothesis": "y"}
    )
    judge.save(fourth, grading.saved(fourth), unlabeled)
    _edit(
        fourth,
        judge.SAVED,
        lambda saved: saved.update(diagnosis_sha256=grading.digest(grading.saved(fourth).diagnosis)),
    )
    found = calibration.report(None)
    said = found["agreement"]["runs"]
    assert (said["count"], said["judged"], said["labeled"]) == (13, 2, 1)
    assert found["runs"][2]["problems"] == ["the judge graded another diagnosis than this run's: not counted"]
    assert found["runs"][1]["problems"] == ["Aravind's verdict is on another diagnosis than this run's: not counted"]
    assert found["runs"][1]["aravind"] is None and found["runs"][2]["judge"] is None
    # Agreement only on the run he labeled: the fourth's judgment tells him nothing yet.
    assert said["rule_with_judge"] == {"agree": 1, "of": 1} and said["judge_with_aravind"] == {"agree": 1, "of": 1}
    assert said["rule_with_aravind"] == {"agree": 1, "of": 1} and said["rule_fix_with_aravind"] == {"agree": 1, "of": 1}
    page = calibration.markdown(found)
    assert "| 2026-10-18T112951Z_loop_ledgerlaghigh | false-alarm | misses the cause (v4), fix False |" in page
    held = f"{calibration.HELD_BACK} | {calibration.HELD_BACK} | - |"
    assert f"| 2026-10-19T024001Z_fault-run_provider-outage | provider-outage | {held}" in page  # judged, not labeled
    assert (
        f"| 2026-10-19T022933Z_fault-run_duplicate-delivery | duplicate-delivery | {calibration.HELD_BACK} | - | - |"
        in page
    )
    assert "partly" not in page.split("## The 13 saved diagnoses")[1]  # no grader's verdict on a run he has not labeled
    assert "Judged: 2 of 13. Labeled by Aravind: 1 of 13." in page and "Problems:" in page


def _edit(folder, name, change):
    path = folder / name
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


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


SCRIPT = (reply(), reply("partly"), reply("misses the cause", False), reply("names the cause", True))


def test_the_run_command_judges_the_sentences_then_the_saved_runs_and_replays(world, calibrating, capsys):
    assert calibrating(*SCRIPT) == 0
    out = capsys.readouterr().out
    assert (
        "Sentences judged: 2; no verdict: 0." in out and "Saved runs judged: 2; no verdict: 0; passed over: 0." in out
    )
    assert "judged (its verdict is held back until Aravind labels this run)" in out  # the review's finding M6
    for relative in calibration.SAVED_RUNS:  # each run's line says it was judged, and never its verdict
        name = Path(relative).name
        assert f"{name}: judged (its verdict is held back" in out
        assert not any(f"{name}: {verdict}" in out for verdict in judge.VERDICTS)
    folder = calibration.latest()
    saved = json.loads((folder / calibration.CALIBRATION).read_text())
    assert saved["label"] == calibration.LABEL and len(saved["sentences"]) == 2 and saved["stopped"] is None
    assert saved["judge"] == {"model": "claude-opus-5-5", "version": 1, "sha256": judge.fingerprint()}
    records = read_records(folder / calibration.RECORDING)
    assert records[0]["what"] == calibration.WHAT and [r["kind"] for r in records].count("response") == 2
    for relative in calibration.SAVED_RUNS:
        assert judge.read(world / relative)["judgment"]["verdict"] in judge.VERDICTS
    assert calibration.main(["replay"]) == 0
    assert capsys.readouterr().out.startswith("Reproduced: the same prompts, replies and judgments as recorded")
    assert calibration.main(["report", "--write"]) == 0
    assert (folder / "calibration-report.md").read_text().startswith("# Klaxon's graders, checked")
    assert not (folder / "calibration-report.json").exists()  # held back: he has labeled none of the runs yet


def test_a_calibration_whose_call_gives_no_reply_stops_and_judges_no_saved_run(world, calibrating, capsys):
    assert calibrating(reply(), ModelTimeout("no reply within 300 s")) == 1
    folder = calibration.latest()
    saved = json.loads((folder / calibration.CALIBRATION).read_text())
    assert len(saved["sentences"]) == 1
    assert saved["stopped"] == "at sentence 2: the call gave no reply Klaxon could use (ModelTimeout)"
    assert "Stopped at sentence 2" in capsys.readouterr().err
    assert not any((world / relative / judge.SAVED).exists() for relative in calibration.SAVED_RUNS)
    assert calibration.replay(folder) == []  # it stops at the same call again


def test_a_replay_finds_a_changed_judgment(world, calibrating, capsys):
    assert calibrating(*SCRIPT) == 0
    path = calibration.latest() / calibration.CALIBRATION
    saved = json.loads(path.read_text())
    saved["sentences"][1]["judgment"]["verdict"] = "names the cause"
    path.write_text(json.dumps(saved))
    assert calibration.replay(calibration.latest()) == ["sentence 2: the judgment differs"]


def test_a_replay_finds_a_changed_judgment_of_a_saved_run(world, calibrating, capsys):
    assert calibrating(*SCRIPT) == 0
    run = world / calibration.SAVED_RUNS[1]
    _edit(run, judge.SAVED, lambda saved: saved["judgment"].update(verdict="partly"))
    assert calibration.replay(calibration.latest()) == [
        f"{calibration.SAVED_RUNS[1]}: the saved judgment differs in: judgment"
    ]


def test_a_replay_finds_a_recorded_call_it_did_not_ask_for_again(world, calibrating):
    assert calibrating(*SCRIPT) == 0
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
    assert capsys.readouterr().out.startswith(
        "No calibration folder yet: the rule on the sentences, and the saved runs."
    )
    assert calibration.main(["replay"]) == 2
    assert "no calibration folder yet in evidence/week5" in capsys.readouterr().err
    assert calibration.main(["report", str(world)]) == 2
    assert "holds no calibration.json" in capsys.readouterr().err


# The live calibration (2026-11-05 UTC, with Aravind's OK): recorded responses from claude-opus-5-5, replayed here
# offline. Its verdicts on the 13 saved runs are not pinned here: they stay held back until Aravind labels the runs.

LIVE = REPO / "evidence" / "week5" / "2026-11-05T025021Z_calibration"


def test_the_live_calibration_replays_exactly_every_sentence_and_saved_run():
    assert calibration.replay(LIVE) == []


@pytest.mark.parametrize("relative", calibration.SAVED_RUNS)
def test_each_saved_runs_live_judgment_replays_exactly_and_came_from_the_judge_as_committed(relative):
    folder = REPO / relative
    assert judge.replay(folder) == []
    item = grading.saved(folder)
    judged, problem = judge.counted(folder, item)
    assert problem is None and judged["judgment"]["verdict"] in judge.VERDICTS  # a verdict, which one held back
    records = [r for r in read_records(folder / judge.RECORDING) if r["kind"] in ("response", "failure")]
    assert [(r["kind"], r["model"], r["source"], r["accepted"]) for r in records] == [
        ("response", "claude-opus-5-5", "claude-code", True)
    ]
    assert (
        records[0]["details"]["claude_code"]["tools"] == []
        and records[0]["details"]["claude_code"]["mcp_servers"] == []
    )


def test_the_live_calibration_on_the_66_sentences():
    # Measured 2026-11-05 UTC (make calibrate): 66 replies, each in the checked shape; the judge agrees with the
    # verdicts Claude expected on 51, and differs by one step, never two: 12 times stricter, 3 times more lenient.
    saved = json.loads((LIVE / calibration.CALIBRATION).read_text())
    assert saved["stopped"] is None and len(saved["sentences"]) == 66
    assert saved["judge"] == {"model": "claude-opus-5-5", "version": 1, "sha256": judge.fingerprint()}
    said = calibration.report(LIVE)["agreement"]["sentences"]
    assert (said["judged"], said["judge_no_verdict"], said["judge_with_expected"]) == (66, 0, {"agree": 51, "of": 66})
    assert said["judge_matrix"] == {
        "names the cause": {"names the cause": 22, "partly": 7, "misses the cause": 0, judge.NO_VERDICT: 0},
        "partly": {"names the cause": 1, "partly": 13, "misses the cause": 5, judge.NO_VERDICT: 0},
        "misses the cause": {"names the cause": 0, "partly": 2, "misses the cause": 16, judge.NO_VERDICT: 0},
    }
    records = [r for r in read_records(LIVE / calibration.RECORDING) if r["kind"] in ("response", "failure")]
    assert len(records) == 66 and {(r["kind"], r["model"], r["accepted"]) for r in records} == {
        ("response", "claude-opus-5-5", True)
    }
