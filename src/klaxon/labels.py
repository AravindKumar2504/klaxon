"""Aravind's own verdicts on Klaxon's diagnoses (week 5, item 2): typed at a terminal, saved beside each run, once.

Both graders, the rule (klaxon.grading) and the judge (klaxon.judge), are checked against these verdicts. So Claude
never writes one: make label reads every answer from a terminal and refuses without one, as make approve does (Claude's
shell has none). Aravind's choice (2026-11-04): blind, one run at a time.

    make label RUN=<a run's folder, or a folder of runs>

For each run not labeled yet, it shows the right answer and the remedy of the system the run was made on, and the whole
diagnosis, in the model's own words. It never shows a grader's verdict, nor which model wrote the diagnosis. Then it
asks: n, p or m (names the cause, partly, misses the cause); y or n (does its fix fit the remedy); an optional sentence
why; and y to save. The verdict is saved as label.json beside the run, written once: it is never overwritten. A run
already labeled, or with no diagnosis, is passed over, and said. Anything but y at the end saves nothing and stops.

Its limit, as the gate's: any process running as Aravind can write label.json, or make itself a terminal (the tests
do). That Claude never labels is a rule; the file records how the verdict was given, not who gave it.

Run: python -m klaxon.labels FOLDER [FOLDER ...]   (make label RUN=FOLDER)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from klaxon import grading
from klaxon.agent import gate
from klaxon.agent.tools import clean_text

LABEL_FILE = "label.json"
LABEL = (
    "A person's verdict on one run's diagnosis (klaxon.labels), typed at a terminal by make label: a person's, not a"
    " rule's or a model's. Shown the right answer and the remedy of the system the run was made on, and the whole"
    " diagnosis; never a grader's verdict, nor which model wrote it."
)
CAUSES = {"n": "names the cause", "p": "partly", "m": "misses the cause"}
FITS = {"y": True, "n": False}
TRIES = 3  # a question is asked at most this often, when the answer is not one it takes
MAX_WHY = 500  # characters kept of the sentence why
STOPPED, SAVED, MEANWHILE = "stopped", "saved", "labeled meanwhile"


def _line(text: object, limit: int = 1000) -> str:
    """Text on one line, with hidden characters dropped: nothing in it can pass for a line of the screen's own."""
    return " ".join(clean_text(str(text), limit).split())


def screen(item: grading.Saved, at: int, of: int) -> str:
    """One run, as Aravind reads it before his verdict: the right answer and the remedy, then the diagnosis, apart and
    labeled. Never a grader's verdict, nor the model."""
    right = grading.answer(item.name, item.version)
    found = item.diagnosis or {}
    lines = [
        f"Run {at} of {of}: {_line(item.folder.name, 200)}",
        "",
        "The right answer (written before any run on it):",
        f"  {_line(right.cause, 3000)}",
        "Its remedy:",
        f"  {_line(right.remedy, 3000)}",
        "",
        "The diagnosis, in the model's own words (it read untrusted tool output: check it):",
        f"  hypothesis:     {_line(found.get('hypothesis'))}",
        f"  confidence:     {_line(found.get('confidence'), 20)}",
    ]
    for item_ in found.get("evidence") or []:
        lines.append(
            f"  evidence:       {_line(item_.get('call_id'), 8)} {_line(item_.get('tool'), 64)}:"
            f" {_line(item_.get('shows'))}"
        )
    fix = found.get("proposed_fix") or {}
    lines.append(
        f"  its fix:        {_line(fix.get('kind'), 40)} {_line(fix.get('target'), 200)}: {_line(fix.get('summary'))}"
    )
    named = found.get("fix_action")
    if isinstance(named, Mapping):
        lines.append(
            f"  action named:   {_line(named.get('name'), 64)} {_line(json.dumps(named.get('arguments')), 300)}"
        )
    lines += [
        f"  blast radius:   {_line(found.get('blast_radius'))}",
        f"  rollback plan:  {_line(found.get('rollback_plan'))}",
        "",
    ]
    return "\n".join(lines)


def _ask(terminal: gate.Terminal, question: str, allowed: Mapping[str, object]) -> object | None:
    """The answer to question, among allowed: asked again when it is not one of them, TRIES times at most. None at the
    end of input, or after the last try."""
    for _ in range(TRIES):
        answer = terminal.ask(question)
        if answer == "":  # the end of input: no answer
            return None
        key = answer.strip().lower()
        if key in allowed:
            return allowed[key]
        terminal.write(f"Please type one of: {', '.join(allowed)}.\n")
    return None


def label_one(item: grading.Saved, terminal: gate.Terminal, at: int, of: int, now: Callable[[], float]) -> str:
    """Show one run and take Aravind's verdict on it: SAVED, STOPPED (nothing saved), or MEANWHILE (a label was
    written by something else while he typed: nothing was written)."""
    terminal.write(screen(item, at, of))
    verdict = _ask(terminal, "Cause: n names it, p partly, m misses it: ", CAUSES)
    fits = None if verdict is None else _ask(terminal, "Does its fix fit the remedy? y or n: ", FITS)
    if fits is None:
        terminal.write("Nothing saved.\n")
        return STOPPED
    why = _line(terminal.ask("Why, in one sentence (Enter to skip): "), MAX_WHY)
    fit = "fits" if fits else "does not fit"
    sure = terminal.ask(f'Save "{verdict}", and the fix {fit}? Type y to save it; anything else saves nothing: ')
    if sure.strip().lower() != "y":
        terminal.write("Nothing saved.\n")
        return STOPPED
    right = grading.answer(item.name, item.version)
    value = {
        "label": LABEL,
        "graded": {"name": item.name, "rubric_version": item.version},  # whose right answer: the system it ran on
        "right_answer_sha256": grading.digest([right.cause, right.remedy]),
        "diagnosis_sha256": grading.digest(item.diagnosis),
        "verdict": verdict,
        "fix_fits": fits,
        "why": why or None,
        "at": gate.utc(now()),
        "how": gate.TYPED,
    }
    if gate._create_once(item.folder / LABEL_FILE, value) is None:
        terminal.write("It was labeled meanwhile: nothing was written.\n")
        return MEANWHILE
    terminal.write(f"Saved: {LABEL_FILE}\n\n")
    return SAVED


def label(
    paths: Sequence[Path],
    open_terminal: Callable[[], gate.Terminal | None] = gate.Terminal.open,
    now: Callable[[], float] = time.time,
) -> int:
    """make label: 0 when every run asked about was labeled, or none was left; 1 when it stopped before the end; 2
    when it was refused, or found no saved run."""
    terminal = open_terminal()
    if terminal is None:
        print(
            "Refused: make label reads your verdicts from a terminal, and this process has none. Verdicts are typed by"
            " a person.",
            file=sys.stderr,
        )
        return 2
    folders = grading.run_folders(paths)
    if not folders:
        terminal.write("No saved run in: " + ", ".join(_line(path, 300) for path in paths) + "\n")
        return 2
    todo = []
    for folder in folders:
        try:
            item = grading.saved(folder)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            terminal.write(f"{_line(folder.name, 200)}: passed over: {_line(exc, 300)}\n")
            continue
        if item.diagnosis is None:
            terminal.write(f"{_line(folder.name, 200)}: passed over: it ended in no diagnosis\n")
        elif (folder / LABEL_FILE).exists():
            terminal.write(f"{_line(folder.name, 200)}: already labeled\n")
        else:
            todo.append(item)
    if not todo:
        terminal.write("Nothing left to label.\n")
        return 0
    terminal.write(
        f"\n{len(todo)} to label. Each shows the right answer, its remedy and the diagnosis: never a grader's verdict,"
        " nor the model.\n\n"
    )
    for at, item in enumerate(todo, 1):
        if label_one(item, terminal, at, len(todo), now) == STOPPED:
            terminal.write(f"Stopped: {at - 1} of {len(todo)} labeled. Run it again to go on.\n")
            return 1
    terminal.write("Done.\n")
    return 0


def read(folder: Path) -> dict | None:
    """A run's saved verdict, or None when it has none. ValueError when the file is not a verdict make label writes."""
    path = Path(folder) / LABEL_FILE
    if not path.is_file():
        return None
    saved = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(saved, dict)
        or saved.get("verdict") not in CAUSES.values()
        or type(saved.get("fix_fits")) is not bool  # not 1 or 0, which equal True and False
        or saved.get("how") != gate.TYPED
    ):
        raise ValueError(f"{Path(folder).name}'s {LABEL_FILE} is not a verdict make label writes")
    return saved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m klaxon.labels", description=__doc__.splitlines()[0])
    parser.add_argument("folders", type=Path, nargs="+", help="run folders, or folders of them (make label RUN=...)")
    args = parser.parse_args(argv)
    return label(args.folders)


if __name__ == "__main__":
    raise SystemExit(main())
