"""Aravind's marks on a postmortem draft (week 5, item 4): for each paragraph of its narrative, right or wrong, and why
when wrong. Typed in his own terminal, and refused without one, as make approve and make label are.

A draft's narrative was written by Claude; its facts were filled in by code (klaxon.agent.postmortem). The draft's own
check reads its numbers, not its claims (known gap 8a): his marks are the check for claims. The screen shows each
paragraph with the facts it draws on, and never what Claude found wrong in a draft before, so his check is his own.

His marks on a draft are saved beside it once, in postmortem-review.json, with a page, postmortem-reviewed.md: the
draft's own page with his mark under each paragraph. The draft itself (postmortem.json, postmortem.md) never changes.
Stopping partway saves nothing for that draft; run it again to go on. A draft already reviewed is passed over.

Run: python -m klaxon.agent.postmortem_review RUN... (make review-postmortem RUN=...)."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from klaxon import grading
from klaxon.agent import gate, postmortem
from klaxon.agent.tools import clean_text
from klaxon.labels import _ask, _line

REVIEW, REVIEWED = "postmortem-review.json", "postmortem-reviewed.md"
MARKS = {"r": "right", "w": "wrong"}
TRIES = 3
MAX_WHY = 500
MAX_FACT = 300  # characters of one fact on the screen
# Each paragraph's heading on the draft's page (klaxon.agent.postmortem.render), and the facts it draws on.
HEADINGS = {
    "summary": "## Summary",
    "timeline": "## Timeline",
    "impact": "## Impact",
    "cause": "## Cause",
    "response": "## Response",
    "repairs": "## Repairs still owed",
}
DRAWS_ON = {
    "summary": ("run", "cause", "action", "verification"),
    "timeline": ("timeline", "durations"),
    "impact": ("impact",),
    "cause": ("cause",),
    "response": ("action", "verification"),
    "repairs": ("repairs",),
}
SAVED, STOPPED, MEANWHILE = "saved", "stopped", "meanwhile"
# His request (2026-11-07): his marks given in the chat, and entered by Claude, word for word. Recorded as such:
# weaker proof that they are his than marks typed at his terminal, and never shown as typed.
CHAT = "given by Aravind in the chat, entered by Claude"
HOWS = {gate.TYPED: "make review-postmortem", CHAT: "his marks given in the chat, entered by Claude"}
# The label each review carries, by how its marks came: never one that says typed when they were not. (Week 5's
# review found the six reviews given in the chat labeled as typed; corrected once at Aravind's OK, 2026-11-08.)
LABELS = {
    gate.TYPED: (
        "Aravind's marks on one postmortem draft: for each paragraph of its narrative, written by Claude, right or wrong,"
        " and why when wrong; typed by him at a terminal (make review-postmortem). The draft beside it is unchanged."
    ),
    CHAT: (
        "Aravind's marks on one postmortem draft: for each paragraph of its narrative, written by Claude, right or wrong,"
        " and why when wrong; given by him in the chat and entered by Claude word for word, at his request. The draft"
        " beside it is unchanged."
    ),
}


class NoReview(ValueError):
    """A folder whose draft cannot be reviewed: no narrative, or a page that does not match its draft."""


def digest(saved: Mapping) -> str:
    """What his marks are on: the draft's facts and narrative."""
    return grading.digest({"facts": saved["facts"], "narrative": saved["draft"]["narrative"]})


def load(folder: Path) -> dict:
    """A draft to review: its saved postmortem.json, with a narrative, and its page, each paragraph where it should be.
    NoReview, and why, when it cannot be reviewed."""
    try:
        saved = json.loads((folder / postmortem.SAVED).read_text(encoding="utf-8"))
        page = (folder / postmortem.MARKDOWN).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise NoReview(f"its draft cannot be read ({type(exc).__name__})") from None
    narrative = (saved.get("draft") or {}).get("narrative") if isinstance(saved, dict) else None
    if not isinstance(narrative, dict) or set(narrative) != set(postmortem.SECTIONS):
        raise NoReview("its draft has no narrative to review")
    _places(saved, page)  # NoReview when the page does not match the draft
    return saved


def _places(saved: Mapping, page: str) -> dict[str, int]:
    """Where each paragraph sits on the page: the line after its heading's blank line, holding it as the page shows it."""
    lines, allowed = page.split("\n"), postmortem.evidence_numbers(saved["facts"])
    places = {}
    for name, heading in HEADINGS.items():
        at = lines.index(heading) + 2 if heading in lines else -1
        shown = postmortem._marked(saved["draft"]["narrative"][name], allowed)
        if at < 2 or at >= len(lines) or lines[at] != shown:
            raise NoReview(f"its page does not show the draft's {name} paragraph where it should")
        places[name] = at
    return places


def _short(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, Mapping | list):
        value = json.dumps(value, ensure_ascii=False)
    return _line(value, MAX_FACT)


def fact_lines(value: object, indent: str = "    ") -> list[str]:
    """Facts as the screen shows them: one line each, nested by indent; a list of rows, one row a line."""
    if isinstance(value, Mapping):
        out = []
        for key, item in value.items():
            if key == "label":  # what the facts are, said once above them
                continue
            if isinstance(item, Mapping | list) and item:
                out += [f"{indent}{_line(key, 60)}:", *fact_lines(item, indent + "  ")]
            else:
                out.append(f"{indent}{_line(key, 60)}: {_short(item)}")
        return out
    if isinstance(value, list):
        return [
            indent
            + "- "
            + (
                "; ".join(f"{_line(k, 60)}: {_short(v)}" for k, v in item.items())
                if isinstance(item, Mapping)
                else _short(item)
            )
            for item in value
        ]
    return [indent + _short(value)]


def screen(folder: Path, saved: Mapping, name: str, at: int, of: int, part: int) -> str:
    """One paragraph, as Aravind reads it before his mark: the paragraph, then the facts it draws on, apart."""
    run = saved["facts"]["run"]
    text = " ".join(clean_text(str(saved["draft"]["narrative"][name]), 20_000).split())
    lines = [
        "",
        f"Draft {at} of {of}: {_line(run.get('title'), 120)} ({_line(run.get('fault'), 60)}), run {_line(folder.name, 80)}",
        f"Paragraph {part} of {len(HEADINGS)}: {name}. {postmortem.SECTIONS[name]}",
        "",
        "The paragraph, as Claude wrote it:",
        *textwrap.wrap(text, 100, initial_indent="  ", subsequent_indent="  "),
        "",
        "The facts it draws on, filled in by code from the run's saved evidence:",
    ]
    for key in DRAWS_ON[name]:
        lines += [f"  {key}:", *fact_lines(saved["facts"].get(key))]
    return "\n".join(lines) + "\n\n"


def _why(terminal: gate.Terminal, wrong: bool) -> tuple[bool, str | None]:
    """His sentence: optional when the mark is right (Enter skips it, as in make label); required when it is wrong,
    asked again TRIES times at most. (False, None) when a wrong mark is left without one, or at the end of input."""
    if not wrong:
        return True, _line(terminal.ask("Why, in one sentence (Enter to skip): "), MAX_WHY) or None
    for _ in range(TRIES):
        answer = terminal.ask("What is wrong, in one sentence: ")
        if answer == "":  # the end of input
            return False, None
        if _line(answer, MAX_WHY):
            return True, _line(answer, MAX_WHY)
        terminal.write("A wrong mark needs one sentence on what is wrong.\n")
    return False, None


def review_one(
    folder: Path, saved: Mapping, terminal: gate.Terminal, at: int, of: int, now: Callable[[], float]
) -> str:
    """His marks on one draft, paragraph by paragraph: SAVED, STOPPED (nothing saved), or MEANWHILE (a review was
    written by something else while he typed: nothing was written)."""
    marks = []
    for part, name in enumerate(HEADINGS, 1):
        terminal.write(screen(folder, saved, name, at, of, part))
        mark = _ask(terminal, "Right or wrong? r or w: ", MARKS)
        answered, why = (False, None) if mark is None else _why(terminal, mark == "wrong")
        if not answered:
            terminal.write("Nothing saved for this draft.\n")
            return STOPPED
        marks.append({"paragraph": name, "mark": mark, "why": why})
    wrong = sum(1 for item in marks if item["mark"] == "wrong")
    sure = terminal.ask(
        f"Save your {len(marks)} marks on this draft ({len(marks) - wrong} right, {wrong} wrong)? Type y to save them;"
        " anything else saves nothing: "
    )
    if sure.strip().lower() != "y":
        terminal.write("Nothing saved for this draft.\n")
        return STOPPED
    if record(folder, saved, marks, gate.TYPED, now) == MEANWHILE:
        terminal.write("It was reviewed meanwhile: nothing was written.\n")
        return MEANWHILE
    terminal.write(f"Saved: {REVIEW} and {REVIEWED}\n")
    return SAVED


def check(marks: Sequence[Mapping]) -> None:
    """ValueError unless marks are one per paragraph, in the draft's order, each right or wrong, a wrong one with
    its sentence."""
    if [item.get("paragraph") for item in marks] != list(HEADINGS):
        raise ValueError(f"marks must be one per paragraph, in order: {', '.join(HEADINGS)}")
    for item in marks:
        if item.get("mark") not in MARKS.values():
            raise ValueError(f"{item['paragraph']}: a mark is right or wrong")
        if item["mark"] == "wrong" and not item.get("why"):
            raise ValueError(f"{item['paragraph']}: a wrong mark needs one sentence on what is wrong")


def record(folder: Path, saved: Mapping, marks: Sequence[Mapping], how: str, now: Callable[[], float]) -> str:
    """His marks on a draft, saved once beside it with their page: SAVED, or MEANWHILE when a review was written
    first (nothing is written). how says how they came: typed at his terminal, or given in the chat and entered by
    Claude."""
    check(marks)
    if how not in HOWS:
        raise ValueError(f"how marks came must be one of: {', '.join(HOWS)}")
    value = {
        "label": LABELS[how],
        "draft": {"run": folder.name, "sha256": digest(saved)},
        "marks": [{"paragraph": m["paragraph"], "mark": m["mark"], "why": m.get("why")} for m in marks],
        "at": gate.utc(now()),
        "how": how,
    }
    if gate._create_once(folder / REVIEW, value) is None:
        return MEANWHILE
    (folder / REVIEWED).write_text(page(folder, saved, value), encoding="utf-8")
    return SAVED


def page(folder: Path, saved: Mapping, review: Mapping) -> str:
    """The draft's own page, with his mark under each paragraph and a note at the top; nothing of the draft changed."""
    lines = (folder / postmortem.MARKDOWN).read_text(encoding="utf-8").split("\n")
    places = _places(saved, "\n".join(lines))
    marks = {item["paragraph"]: item for item in review["marks"]}
    for name, at in sorted(places.items(), key=lambda pair: -pair[1]):  # from the bottom up: places stay right
        item = marks[name]
        said = f": {postmortem.md(item['why'])}" if item["why"] else "."
        lines[at + 1 : at + 1] = ["", f"> **Aravind's mark:** {item['mark']}{said}"]
    wrong = sum(1 for item in review["marks"] if item["mark"] == "wrong")
    note = (
        f"> Reviewed by Aravind, paragraph by paragraph, at {review['at']} ({HOWS[review['how']]}):"
        f" {len(review['marks']) - wrong} right, {wrong} wrong. His mark is under each paragraph; the draft is"
        " otherwise exactly as received."
    )
    return "\n".join([lines[0], "", note, *lines[1:]])


def read(folder: Path) -> dict | None:
    """A draft's saved review, or None when it has none. ValueError when the file is not one this command writes."""
    path = folder / REVIEW
    if not path.exists():
        return None
    saved = json.loads(path.read_text(encoding="utf-8"))
    marks = saved.get("marks") if isinstance(saved, dict) else None
    if (
        not isinstance(marks, list)
        or saved.get("how") not in HOWS
        or saved.get("label") != LABELS[saved["how"]]
        or [item.get("paragraph") for item in marks] != list(HEADINGS)
        or any(item.get("mark") not in MARKS.values() for item in marks)
        or any(item["mark"] == "wrong" and not item.get("why") for item in marks)
    ):
        raise ValueError(f"{folder.name}'s {REVIEW} is not a review make review-postmortem writes")
    return saved


def counted(folder: Path) -> tuple[dict | None, str | None]:
    """A draft's review, if it is on this very draft. Else None, and why (no why when there is no review)."""
    try:
        saved = read(folder)
        draft = json.loads((folder / postmortem.SAVED).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"its {REVIEW} cannot be read ({type(exc).__name__}): not counted"
    if saved is None:
        return None, None
    if saved["draft"].get("sha256") != digest(draft):
        return None, "his marks are on another draft than this one: not counted"
    return saved, None


def _drafts(paths: Sequence[Path]) -> list[Path]:
    """The folders holding a draft: each path, or the run folders in it."""
    found = [path for path in paths if (path / postmortem.SAVED).is_file()]
    found += [folder for folder in grading.run_folders(paths) if (folder / postmortem.SAVED).is_file()]
    return sorted(set(found))


def review(
    paths: Sequence[Path],
    open_terminal: Callable[[], gate.Terminal | None] = gate.Terminal.open,
    now: Callable[[], float] = time.time,
) -> int:
    """make review-postmortem: 0 when every draft asked about was reviewed, or none was left; 1 when it stopped before
    the end; 2 when it was refused, or found no draft."""
    terminal = open_terminal()
    if terminal is None:
        print(
            "Refused: make review-postmortem reads your marks from a terminal, and this process has none. Marks are"
            " typed by a person.",
            file=sys.stderr,
        )
        return 2
    folders = _drafts(paths)
    if not folders:
        terminal.write("No postmortem draft in: " + ", ".join(_line(path, 300) for path in paths) + "\n")
        return 2
    todo = []
    for folder in folders:
        if (folder / REVIEW).exists():
            terminal.write(f"{_line(folder.name, 200)}: already reviewed\n")
            continue
        try:
            todo.append((folder, load(folder)))
        except NoReview as exc:
            terminal.write(f"{_line(folder.name, 200)}: passed over: {_line(exc, 300)}\n")
    if not todo:
        terminal.write("Nothing left to review.\n")
        return 0
    terminal.write(
        f"\n{len(todo)} drafts to review, {len(HEADINGS)} paragraphs each. Each paragraph shows with the facts it"
        " draws on, never a finding about it.\n"
    )
    for at, (folder, saved) in enumerate(todo, 1):
        if review_one(folder, saved, terminal, at, len(todo), now) == STOPPED:
            terminal.write(f"Stopped: {at - 1} of {len(todo)} drafts reviewed. Run it again to go on.\n")
            return 1
    terminal.write("Done.\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m klaxon.agent.postmortem_review", description=__doc__.split("\n")[0]
    )
    parser.add_argument("runs", type=Path, nargs="+", help="run folders with a draft, or folders of them")
    args = parser.parse_args(argv)
    return review(args.runs)


if __name__ == "__main__":
    sys.exit(main())
