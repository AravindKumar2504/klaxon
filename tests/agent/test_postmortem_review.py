"""Aravind's marks on a postmortem draft (klaxon.agent.postmortem_review), offline: refused without a terminal, as make
label is; each paragraph shown with the facts it draws on, never a finding about it; a wrong mark needs a sentence;
saved once beside the draft, with a page that has his mark under each paragraph; the draft itself never changes.

The terminal is scripted (ScriptedTerminal, from the gate's tests): a scripted reviewer, not a person, and every
answer in these tests is made up. Two tests run the real command: with no terminal, refused as Claude's shell is; in
a pseudo-terminal the test makes. The drafts are copies of week 4's saved drafts: a test never changes the evidence."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from klaxon.agent import gate, postmortem
from klaxon.agent import postmortem_review as review
from klaxon.patient.stack import REPO
from tests.agent.test_gate import ScriptedTerminal, _exit_code, _read_until

WEEK4 = REPO / "evidence" / "week4"
FAULT_1 = WEEK4 / "2026-10-21T090513Z_gated-run_duplicate-delivery"
NOW = 1_793_000_000.0  # 2026-10-26T07:33:20Z, a made-up clock
WRONG = "It says 12 payments differed, where the facts show 12 payments whose books differed."  # made up
ALL_RIGHT = ("r", "") * 6 + ("y",)  # each paragraph right, no sentence; then save


def copy_draft(source: Path, parent: Path) -> Path:
    """A copy of a run with its draft, as it was before any review (and without the graders' files)."""
    ignore = shutil.ignore_patterns(
        review.REVIEW, review.REVIEWED, "judgment.json", "judge-recording.jsonl", "label.json"
    )
    return Path(shutil.copytree(source, parent / source.name, ignore=ignore))


def marks(folder, *answers, during=None):
    terminal = ScriptedTerminal(*answers, during=during)
    code = review.review([folder], lambda: terminal, lambda: NOW)
    return code, "".join(terminal.shown)


def test_without_a_terminal_it_refuses_and_writes_nothing(tmp_path, capsys):
    folder = copy_draft(FAULT_1, tmp_path)
    assert review.review([folder], lambda: None) == 2
    assert "Refused: make review-postmortem reads your marks from a terminal" in capsys.readouterr().err
    assert not (folder / review.REVIEW).exists() and not (folder / review.REVIEWED).exists()


def test_marks_typed_at_a_terminal_are_saved_once_with_a_page_and_the_draft_is_unchanged(tmp_path):
    folder = copy_draft(FAULT_1, tmp_path)
    before = {name: (folder / name).read_bytes() for name in (postmortem.SAVED, postmortem.MARKDOWN)}
    answers = ("r", "", "w", WRONG, "R", "Every number matches the facts.", "r", "", "r", "", "r", "", "y")
    code, shown = marks(folder, *answers)
    assert (
        code == 0 and "Saved: postmortem-review.json and postmortem-reviewed.md" in shown and shown.endswith("Done.\n")
    )
    assert "Save your 6 marks on this draft (5 right, 1 wrong)?" in shown
    saved = review.read(folder)
    draft = json.loads(before[postmortem.SAVED])
    assert saved == {
        "label": review.LABEL,
        "draft": {"run": folder.name, "sha256": review.digest(draft)},
        "marks": [
            {"paragraph": "summary", "mark": "right", "why": None},
            {"paragraph": "timeline", "mark": "wrong", "why": WRONG},
            {"paragraph": "impact", "mark": "right", "why": "Every number matches the facts."},
            {"paragraph": "cause", "mark": "right", "why": None},
            {"paragraph": "response", "mark": "right", "why": None},
            {"paragraph": "repairs", "mark": "right", "why": None},
        ],
        "at": "2026-10-26T07:33:20Z",
        "how": gate.TYPED,
    }
    assert {name: (folder / name).read_bytes() for name in before} == before  # the draft is exactly as received
    page = (folder / review.REVIEWED).read_text().split("\n")
    original = before[postmortem.MARKDOWN].decode().split("\n")
    assert page[0] == original[0] and page[2].startswith("> Reviewed by Aravind, paragraph by paragraph, at 2026-10-26")
    assert "5 right, 1 wrong" in page[2]
    # His mark sits right under its paragraph; take the marks and the note out, and the draft's page is left whole.
    at = page.index(original[original.index("## Timeline") + 2])
    assert page[at + 1 : at + 3] == ["", f"> **Aravind's mark:** wrong: {postmortem.md(WRONG)}"]
    assert sum(1 for line in page if line.startswith("> **Aravind's mark:** ")) == 6
    kept = [line for i, line in enumerate(page) if i not in (1, 2) and not line.startswith("> **Aravind's mark:** ")]
    assert "\n".join(kept).replace("\n\n\n", "\n\n") == "\n".join(original).replace("\n\n\n", "\n\n")
    assert review.counted(folder)[0] == saved


def test_each_paragraph_shows_with_the_facts_it_draws_on_and_never_a_finding(tmp_path):
    folder = copy_draft(FAULT_1, tmp_path)
    draft = json.loads((folder / postmortem.SAVED).read_text())
    code, shown = marks(folder, *ALL_RIGHT)
    assert code == 0
    starts = [shown.index(f"Paragraph {part} of 6: {name}.") for part, name in enumerate(review.HEADINGS, 1)]
    assert starts == sorted(starts)  # in the draft's own order
    cause = shown[starts[3] : starts[4]]
    assert " ".join(draft["draft"]["narrative"]["cause"].split())[:60] in " ".join(cause.split())
    assert "The facts it draws on, filled in by code" in cause and "recorded:" in cause and "timeline:" not in cause
    lowered = shown.lower()
    for word in ("flagged", "problems", postmortem.FLAG.lower(), "found wrong", "known gap"):
        assert word not in lowered, word


@pytest.mark.parametrize(
    "answers",
    [
        ("w", "\n", "  \n", "\n"),  # a wrong mark with no sentence, three times
        ("w",),  # the end of input at the sentence
        ("x", "y", "z"),  # three answers it cannot take
        (),  # the end of input at once
        ALL_RIGHT[:-1] + ("n",),  # anything but y at the end
        ALL_RIGHT[:-1],  # the end of input at the end
    ],
)
def test_stopping_partway_or_anything_but_y_saves_nothing(tmp_path, answers):
    folder = copy_draft(FAULT_1, tmp_path)
    code, shown = marks(folder, *answers)
    assert code == 1 and "Nothing saved for this draft." in shown and "Stopped: 0 of 1 drafts reviewed" in shown
    assert not (folder / review.REVIEW).exists() and not (folder / review.REVIEWED).exists()
    if answers[:2] == ("w", "\n"):
        assert shown.count("A wrong mark needs one sentence on what is wrong.") == 3


def test_a_review_is_never_overwritten_and_a_reviewed_draft_is_passed_over(tmp_path):
    folder = copy_draft(FAULT_1, tmp_path)
    assert marks(folder, *ALL_RIGHT)[0] == 0
    before = (folder / review.REVIEW).read_bytes()
    code, shown = marks(folder, *(("w", WRONG) * 6 + ("y",)))
    assert code == 0 and "already reviewed" in shown and "Nothing left to review." in shown
    assert (folder / review.REVIEW).read_bytes() == before


def test_a_review_written_meanwhile_is_kept_and_nothing_is_written(tmp_path):
    folder = copy_draft(FAULT_1, tmp_path)

    def meanwhile():
        if not (folder / review.REVIEW).exists():
            (folder / review.REVIEW).write_text('{"written": "meanwhile"}')

    code, shown = marks(folder, *ALL_RIGHT, during=meanwhile)
    assert code == 0 and "It was reviewed meanwhile: nothing was written." in shown
    assert json.loads((folder / review.REVIEW).read_text()) == {"written": "meanwhile"}
    assert not (folder / review.REVIEWED).exists()


def test_the_six_week_4_drafts_load_in_order_and_a_draft_that_cannot_be_reviewed_is_passed_over(tmp_path):
    folders = review._drafts([WEEK4])
    assert len(folders) == 6 and folders == sorted(folders)
    assert all(review.load(folder)["draft"]["narrative"] for folder in folders)
    shifted = copy_draft(FAULT_1, tmp_path / "a")
    page = (shifted / postmortem.MARKDOWN).read_text()
    (shifted / postmortem.MARKDOWN).write_text(page.replace("## Summary\n\n", "## Summary\n\nAn added line.\n", 1))
    bare = copy_draft(FAULT_1, tmp_path / "b")
    draft = json.loads((bare / postmortem.SAVED).read_text())
    (bare / postmortem.SAVED).write_text(json.dumps(draft | {"draft": draft["draft"] | {"narrative": None}}))
    code, shown = marks(tmp_path)
    assert code == 0 and "Nothing left to review." in shown
    assert f"{shifted.name}: passed over: its page does not show the draft's summary paragraph where it should" in shown
    assert f"{bare.name}: passed over: its draft has no narrative to review" in shown
    assert review.review([tmp_path / "nothing"], lambda: ScriptedTerminal()) == 2


def test_a_review_counts_only_on_this_very_draft(tmp_path):
    folder = copy_draft(FAULT_1, tmp_path)
    assert review.counted(folder) == (None, None)
    assert marks(folder, *ALL_RIGHT)[0] == 0
    assert review.counted(folder)[0]["marks"][0]["mark"] == "right"
    draft = json.loads((folder / postmortem.SAVED).read_text())
    draft["draft"]["narrative"]["cause"] += " Changed afterwards."
    (folder / postmortem.SAVED).write_text(json.dumps(draft))
    assert review.counted(folder) == (None, "his marks are on another draft than this one: not counted")
    (folder / review.REVIEW).write_text("{")
    assert review.counted(folder) == (None, "its postmortem-review.json cannot be read (JSONDecodeError): not counted")


def test_a_saved_file_that_is_not_a_review_this_command_writes_is_refused(tmp_path):
    good = [{"paragraph": name, "mark": "right", "why": None} for name in review.HEADINGS]
    for bad in (
        {"marks": good, "how": "written by a script"},
        {"marks": good[:5], "how": gate.TYPED},
        {"marks": [dict(good[0], mark="maybe"), *good[1:]], "how": gate.TYPED},
        {"marks": [dict(good[0], mark="wrong"), *good[1:]], "how": gate.TYPED},  # wrong, with no sentence
        [],
    ):
        (tmp_path / review.REVIEW).write_text(json.dumps(bad))
        with pytest.raises(ValueError, match="is not a review make review-postmortem writes"):
            review.read(tmp_path)


# The real command: no terminal, then a pseudo-terminal


def test_the_command_with_no_terminal_is_refused_as_claudes_shell_is(tmp_path):
    folder = copy_draft(FAULT_1, tmp_path)
    done = subprocess.run(
        [sys.executable, "-m", "klaxon.agent.postmortem_review", str(folder)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,  # no controlling terminal either
    )
    assert done.returncode == 2 and "Refused: make review-postmortem reads your marks from a terminal" in done.stderr
    assert not (folder / review.REVIEW).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal needs a Unix")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_the_command_in_a_terminal_saves_the_typed_marks(tmp_path):
    """A pseudo-terminal the test makes, standing in for Aravind's: the command reads from /dev/tty."""
    import pty

    folder = copy_draft(FAULT_1, tmp_path)
    pid, fd = pty.fork()
    if pid == 0:  # the child: the command, with the pseudo-terminal as its terminal
        os.execv(sys.executable, [sys.executable, "-m", "klaxon.agent.postmortem_review", str(folder)])
    shown = _read_until(fd, b"Right or wrong? r or w: ")
    for part in range(6):
        os.write(fd, b"r\n")
        shown += _read_until(fd, b"Enter to skip): ")
        os.write(fd, b"\n")
        shown += _read_until(fd, b"Type y to save them" if part == 5 else b"Right or wrong? r or w: ")
    os.write(fd, b"y\n")
    shown += _read_until(fd, b"")
    assert _exit_code(pid) == 0, shown
    assert [item["mark"] for item in review.read(folder)["marks"]] == ["right"] * 6
    assert review.read(folder)["how"] == gate.TYPED and (folder / review.REVIEWED).exists()


# Marks given in the chat (his request, 2026-11-07): entered by Claude word for word, and recorded as such


def given(*pairs):
    """Marks as he gives them in the chat, made up for the test: (mark, why) per paragraph, in order."""
    return [
        {"paragraph": name, "mark": mark, "why": why} for name, (mark, why) in zip(review.HEADINGS, pairs, strict=True)
    ]


def test_marks_given_in_the_chat_are_recorded_as_such_once_with_their_page(tmp_path):
    folder = copy_draft(FAULT_1, tmp_path)
    before = {name: (folder / name).read_bytes() for name in (postmortem.SAVED, postmortem.MARKDOWN)}
    marks = given(("right", None), ("wrong", WRONG), ("right", None), ("right", None), ("right", None), ("right", None))
    saved = review.load(folder)
    assert review.record(folder, saved, marks, review.CHAT, lambda: NOW) == review.SAVED
    kept = review.read(folder)
    assert kept["how"] == "given by Aravind in the chat, entered by Claude" and kept["marks"] == marks
    page = (folder / review.REVIEWED).read_text()
    assert "(his marks given in the chat, entered by Claude): 5 right, 1 wrong." in page
    assert "make review-postmortem" not in page.split("\n")[2]  # never shown as typed at his terminal
    assert {name: (folder / name).read_bytes() for name in before} == before
    assert review.record(folder, saved, marks, review.CHAT, lambda: NOW) == review.MEANWHILE  # never overwritten
    assert review.read(folder) == kept and review.counted(folder)[0] == kept


@pytest.mark.parametrize(
    ("marks", "how", "why"),
    [
        (given(*[("right", None)] * 5, ("wrong", None)), review.CHAT, "repairs: a wrong mark needs one sentence"),
        (given(*[("right", None)] * 5, ("maybe", None)), review.CHAT, "repairs: a mark is right or wrong"),
        (given(*[("right", None)] * 6)[::-1], review.CHAT, "marks must be one per paragraph, in order"),
        (given(*[("right", None)] * 6)[:5], review.CHAT, "marks must be one per paragraph, in order"),
        (given(*[("right", None)] * 6), "written by a script", "how marks came must be one of"),
    ],
)
def test_marks_that_are_not_whole_or_say_nothing_of_how_they_came_are_refused(tmp_path, marks, how, why):
    folder = copy_draft(FAULT_1, tmp_path)
    with pytest.raises(ValueError, match=why):
        review.record(folder, review.load(folder), marks, how, lambda: NOW)
    assert not (folder / review.REVIEW).exists() and not (folder / review.REVIEWED).exists()
