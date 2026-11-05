"""Aravind's verdicts (klaxon.labels), offline: refused without a terminal, as make approve is; blind (the right answer,
the remedy and the diagnosis, never a grader's verdict or the model); each answer checked; saved once, beside the run,
never overwritten.

The terminal is scripted (ScriptedTerminal, from the gate's tests): a scripted labeler, not a person. Two tests run the
real command: with no terminal, refused as Claude's shell is; in a pseudo-terminal the test makes, which shows the limit
the docs state. The runs are copies of the saved runs' evidence: a test never changes the evidence itself."""

import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from klaxon import calibration, faults, grading, labels
from klaxon.agent import gate
from klaxon.patient.stack import REPO
from tests.agent.test_gate import ScriptedTerminal, _exit_code, _read_until

FIRST_LIVE_RUN, FAULT_1 = (REPO / calibration.SAVED_RUNS[i] for i in (0, 1))
NOW = 1_793_000_000.0  # 2026-10-26T07:33:20Z, a made-up clock


def copy_run(source: Path, parent: Path) -> Path:
    """A copy of a saved run as it was before any grader (the review's finding H1): no judgment, no verdict of his."""
    ignore = shutil.ignore_patterns("judgment.json", "judge-recording.jsonl", labels.LABEL_FILE)
    return Path(shutil.copytree(source, parent / source.name, ignore=ignore))


def label(folder, *answers, during=None):
    terminal = ScriptedTerminal(*answers, during=during)
    code = labels.label([folder], lambda: terminal, lambda: NOW)
    return code, "".join(terminal.shown)


def test_without_a_terminal_make_label_refuses_and_writes_nothing(tmp_path, capsys):
    folder = copy_run(FAULT_1, tmp_path)
    assert labels.label([folder], lambda: None) == 2
    assert "Refused: make label reads your verdicts from a terminal" in capsys.readouterr().err
    assert not (folder / labels.LABEL_FILE).exists()


def test_a_verdict_typed_at_a_terminal_is_saved_beside_the_run(tmp_path):
    folder = copy_run(FAULT_1, tmp_path)
    code, shown = label(folder, "N", "y", "It names the dedupe gone, and why it books twice.", "y")
    assert code == 0 and "Saved: label.json" in shown and shown.endswith("Done.\n")
    saved = labels.read(folder)
    item = grading.saved(folder)
    right = grading.answer(item.name, item.version)
    assert saved == {
        "label": labels.LABEL,
        "graded": {"name": "duplicate-delivery", "rubric_version": 1},
        "right_answer_sha256": grading.digest([right.cause, right.remedy]),
        "diagnosis_sha256": grading.digest(item.diagnosis),
        "verdict": "names the cause",
        "fix_fits": True,
        "why": "It names the dedupe gone, and why it books twice.",
        "at": "2026-10-26T07:33:20Z",
        "how": "typed at a terminal",
    }


def test_the_screen_shows_the_right_answer_and_the_diagnosis_and_never_a_graders_verdict_or_the_model(tmp_path):
    folder = copy_run(FAULT_1, tmp_path)
    (folder / "judgment.json").write_text('{"judgment": {"verdict": "partly"}}')  # there, and never shown
    code, shown = label(folder, "p", "n", "", "y")
    assert code == 0
    assert faults.FAULTS["duplicate-delivery"].cause in shown and faults.FAULTS["duplicate-delivery"].remedy in shown
    item = grading.saved(folder)
    assert " ".join(item.diagnosis["hypothesis"].split()) in shown
    assert "its fix:        rollback ledger-consumer:" in shown and "evidence:       c1 " in shown
    lowered = shown.lower()
    for word in ("rubric", "judge", "claude", "opus", "sonnet", "names the cause (", "verdict:"):
        assert word not in lowered, word
    assert labels.read(folder)["why"] is None  # Enter: no sentence


def test_week_3s_first_live_run_is_labeled_against_the_false_alarms_right_answer(tmp_path):
    folder = copy_run(FIRST_LIVE_RUN, tmp_path)
    code, shown = label(folder, "m", "n", "It calls an idle consumer hung.", "y")
    assert code == 0 and "Nothing is wrong: the ledger consumer group is not behind" in shown
    assert labels.read(folder)["graded"] == {"name": "false-alarm", "rubric_version": 4}


def test_an_answer_it_cannot_take_is_asked_again_three_times_at_most(tmp_path):
    folder = copy_run(FAULT_1, tmp_path)
    code, shown = label(folder, "maybe", "P", "yes", "n", "", "y")
    assert code == 0 and shown.count("Please type one of: n, p, m.") == 1 and shown.count("Please type one of: y, n.")
    assert (labels.read(folder)["verdict"], labels.read(folder)["fix_fits"]) == ("partly", False)
    folder = copy_run(FIRST_LIVE_RUN, tmp_path)
    code, shown = label(folder, "a", "b", "c", "m")  # three wrong answers: it stops, and the fourth is never read
    assert code == 1 and "Nothing saved." in shown and "Stopped: 0 of 1 labeled" in shown
    assert not (folder / labels.LABEL_FILE).exists()


@pytest.mark.parametrize(
    "answers",
    [
        (),  # the end of input at once
        ("n",),  # at the second question
        ("n", "y", "x"),  # at the confirmation
        ("n", "y", "x", "no"),  # anything but y saves nothing
    ],
)
def test_no_answer_or_anything_but_y_at_the_end_saves_nothing(tmp_path, answers):
    folder = copy_run(FAULT_1, tmp_path)
    code, shown = label(folder, *answers)
    assert code == 1 and "Nothing saved." in shown
    assert not (folder / labels.LABEL_FILE).exists()


def test_a_label_is_never_overwritten(tmp_path):
    folder = copy_run(FAULT_1, tmp_path)
    assert label(folder, "n", "y", "", "y")[0] == 0
    before = (folder / labels.LABEL_FILE).read_bytes()
    code, shown = label(folder, "m", "n", "", "y")
    assert code == 0 and "already labeled" in shown and "Nothing left to label." in shown
    assert (folder / labels.LABEL_FILE).read_bytes() == before


def test_a_label_written_meanwhile_is_kept_and_nothing_is_written(tmp_path):
    folder = copy_run(FAULT_1, tmp_path)

    def meanwhile():
        if not (folder / labels.LABEL_FILE).exists():
            (folder / labels.LABEL_FILE).write_text('{"written": "meanwhile"}')

    code, shown = label(folder, "n", "y", "", "y", during=meanwhile)
    assert code == 0 and "It was labeled meanwhile: nothing was written." in shown
    assert json.loads((folder / labels.LABEL_FILE).read_text()) == {"written": "meanwhile"}


def test_a_folder_of_runs_is_labeled_in_a_shuffled_order_and_passes_over_runs_with_nothing_to_label(tmp_path):
    week = tmp_path / "week"
    runs = [copy_run(REPO / relative, week) for relative in calibration.SAVED_RUNS[:4]]
    empty = week / "2026-10-20T000000Z_fault-run_poison-message"
    empty.mkdir()
    (empty / "run.json").write_text(json.dumps({"fault": {"name": "poison-message"}}))  # no loop: no diagnosis
    broken = week / "2026-10-20T000001Z_fault-run_poison-message"
    broken.mkdir()
    (broken / "run.json").write_text("[]")
    order = list(runs)
    random.Random(labels.ORDER_SEED).shuffle(order)
    assert order != runs  # the review's finding M6: the order says nothing, not even the week
    code, shown = label(week, *(["p", "y", "", "y"] * 4))
    assert code == 0 and "4 to label, in a shuffled order." in shown
    assert [shown.index(f"Run {n} of 4: {run.name}") for n, run in enumerate(order, 1)] == sorted(
        shown.index(f"Run {n} of 4: {run.name}") for n, run in enumerate(order, 1)
    )
    assert f"{empty.name}: passed over: it ended in no diagnosis" in shown
    assert f"{broken.name}: passed over: {broken.name}'s run.json is not an object" in shown
    assert all(labels.read(run)["verdict"] == "partly" for run in runs)
    assert label(tmp_path / "nothing")[0] == 2


def test_a_report_counts_only_his_verdict_on_this_diagnosis_against_this_right_answer(tmp_path):
    folder = copy_run(FAULT_1, tmp_path)
    assert label(folder, "n", "y", "", "y")[0] == 0
    item = grading.saved(folder)
    assert labels.counted(folder, item)[0]["verdict"] == "names the cause"
    other = grading.Saved(folder, "duplicate-delivery", 1, dict(item.diagnosis, confidence=0.1))
    assert labels.counted(folder, other) == (
        None,
        "Aravind's verdict is on another diagnosis than this run's: not counted",
    )
    saved = labels.read(folder) | {"right_answer_sha256": "another right answer"}
    (folder / labels.LABEL_FILE).write_text(json.dumps(saved))
    assert labels.counted(folder, item) == (
        None,
        "Aravind's verdict was given against another right answer: not counted",
    )
    (folder / labels.LABEL_FILE).write_text("{")
    assert labels.counted(folder, item) == (None, "its label.json cannot be read (JSONDecodeError): not counted")
    assert labels.counted(tmp_path, item) == (None, None)


def test_a_saved_file_that_is_not_a_verdict_make_label_writes_is_refused(tmp_path):
    assert labels.read(tmp_path) is None
    for bad in ({"verdict": "partly", "fix_fits": 1, "how": gate.TYPED}, {"verdict": "yes", "fix_fits": True}, []):
        (tmp_path / labels.LABEL_FILE).write_text(json.dumps(bad))
        with pytest.raises(ValueError, match="is not a verdict make label writes"):
            labels.read(tmp_path)


# The real command: no terminal, then a pseudo-terminal


def test_make_label_with_no_terminal_is_refused_as_claudes_shell_is(tmp_path):
    folder = copy_run(FAULT_1, tmp_path)
    done = subprocess.run(
        [sys.executable, "-m", "klaxon.labels", str(folder)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,  # no controlling terminal either
    )
    assert done.returncode == 2 and "Refused: make label reads your verdicts from a terminal" in done.stderr
    assert not (folder / labels.LABEL_FILE).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal needs a Unix")
# Earlier tests leave threads running; the child runs the command at once (execv), so it takes no lock they hold.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_make_label_in_a_terminal_saves_the_typed_verdict(tmp_path):
    """A pseudo-terminal the test makes, standing in for Aravind's: the command reads from /dev/tty. It also shows the
    limit the docs state: a process can make itself a terminal."""
    import pty

    folder = copy_run(FAULT_1, tmp_path)
    pid, fd = pty.fork()
    if pid == 0:  # the child: the command, with the pseudo-terminal as its terminal
        os.execv(sys.executable, [sys.executable, "-m", "klaxon.labels", str(folder)])
    shown = _read_until(fd, b"Cause: ")
    for answer, next_prompt in ((b"n", b"Does its fix"), (b"y", b"Why, in"), (b"", b"Type y to save"), (b"y", b"")):
        os.write(fd, answer + b"\n")
        shown += _read_until(fd, next_prompt)
    assert _exit_code(pid) == 0, shown
    assert labels.read(folder)["verdict"] == "names the cause" and labels.read(folder)["how"] == gate.TYPED
