"""Safe HTML for the panel: every value from a run becomes text, through one escaping function, never markup.

A run's files hold untrusted text: alerts, tool results, logs and Claude's replies can carry any string, an HTML tag
or an instruction to whoever reads them. esc() turns each into text. Html marks only markup the panel built itself,
with el(); nothing read from a run becomes Html without passing through esc().
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from urllib.parse import quote

from klaxon.agent.tools import clean_text

LIMIT = 20_000  # the most characters of one value a page shows: clean_text says how many more there were
VOID = frozenset({"br", "hr", "meta", "link", "input"})


class Html(str):
    """Markup the panel built itself. Text from a run becomes one only through esc()."""

    __slots__ = ()


def esc(value: object, limit: int = LIMIT) -> Html:
    """The one escaping function. Any value as text: hidden characters dropped, this Mac's home folder written as ~,
    the length capped (klaxon.agent.tools.clean_text, as for the model's prompts), then <, >, &, ' and " escaped."""
    if isinstance(value, Html):
        return value
    return Html(html.escape(clean_text(str(value), limit), quote=True))


def join(*parts: object) -> Html:
    """Parts in order: each escaped, unless the panel built it. None and False are left out; a list is joined."""
    out: list[str] = []
    for part in parts:
        if part is None or part is False:
            continue
        if isinstance(part, Html):
            out.append(part)
        elif isinstance(part, str | int | float | dict):
            out.append(esc(part))
        elif isinstance(part, Iterable):
            out.append(join(*part))
        else:
            out.append(esc(part))
    return Html("".join(out))


def el(tag: str, *children: object, **attrs: object) -> Html:
    """One element. Tag and attribute names come from the panel's code (class_ is class, aria_label is aria-label);
    every attribute value is escaped, and so is every child the panel did not build."""
    said = []
    for name, value in attrs.items():
        if value is None or value is False:
            continue
        name = name.rstrip("_").replace("_", "-")
        said.append(f" {name}" if value is True else f' {name}="{esc(value)}"')
    opening = f"<{tag}{''.join(said)}>"
    if tag in VOID:
        return Html(opening)
    return Html(f"{opening}{join(*children)}</{tag}>")


def href(*segments: str, **query: str) -> str:
    """A link inside the panel: each segment quoted, so a run's name never becomes a path, a host or a script."""
    path = "/" + "/".join(quote(str(segment), safe="") for segment in segments)
    if query:
        path += "?" + "&".join(f"{quote(key, safe='')}={quote(str(value), safe='')}" for key, value in query.items())
    return path


# A small Markdown reader for the text code writes (make eval-report's tables, the postmortem drafts): headings,
# paragraphs, lists, pipe tables, `code` and **bold**. Every piece of text passes through esc(); no HTML in the
# Markdown is ever kept as HTML.

_UNESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text: str) -> Html:
    escaped = esc(_UNESCAPE.sub(r"\1", text))
    return Html(_BOLD.sub(r"<b>\1</b>", _CODE.sub(r"<code>\1</code>", escaped)))


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def markdown(text: str) -> Html:
    """Markdown that code wrote, as HTML: its text escaped, its structure kept."""
    out: list[Html] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")), 4)
            out.append(el(f"h{level + 1}", _inline(line.lstrip("#").strip())))
            i += 1
        elif line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            rows = [_cells(row) for row in block if not set(row.replace("|", "").strip()) <= set("-: ")]
            head, body = rows[0], rows[1:]
            out.append(
                el(
                    "div",
                    el(
                        "table",
                        el("thead", el("tr", [el("th", _inline(cell)) for cell in head])),
                        el("tbody", [el("tr", [el("td", _inline(cell)) for cell in row]) for row in body]),
                    ),
                    class_="scroll",
                )
            )
        elif line.lstrip().startswith(("- ", "* ")):
            items = []
            while i < len(lines) and lines[i].lstrip().startswith(("- ", "* ")):
                items.append(el("li", _inline(lines[i].lstrip()[2:])))
                i += 1
            out.append(el("ul", items))
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith(("#", "|", "- ", "* ")):
                para.append(lines[i].strip())
                i += 1
            out.append(el("p", _inline(" ".join(para))))
    return join(out)
