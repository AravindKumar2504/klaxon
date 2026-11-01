"""The numbers in a text, each with its kind, so that a narrative's numbers can be checked against its facts (week 4,
item 5, klaxon.agent.postmortem).

Each run of the digits 0 to 9 is read as part of exactly one token, and each token as one of these kinds:

    datetime  2026-10-21T09:06:47Z, 2026-10-21 09:06:47 UTC: read as a date and a time
    date      2026-10-21
    time      09:06:47, 09:06:47 UTC, 9:06: to the second, or to the minute
    money     $1,531.87, $40: in cents
    duration  96 s, 45.8 s, 10 minutes, 10-minute, 200 ms, 5 min 23 s: in seconds
    percent   92%, 4.2 percent
    id        r-e16d, evt_000251, pay_000230, p95, 5xx, v2, claude-opus-5-5: a word of letters and digits
    number    1,080, 0.85, 7

A cardinal number written in words (two, ten, hundred, half) is a token too. "One", "once", "twice" and the ordinals
are not read: prose uses them for much else, so they are a stated limit of the check.

Two tokens are the same number when they are of the same kind and stand for the same value: $28.00 is not 28, 96 s is
not 96, and $1,532 is not $1,531.87.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CLOCK = r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
ZONE = r"(?:Z|[ ]?UTC\b)?"
TOKENS = re.compile(
    "|".join(
        f"(?P<{name}>{pattern})"
        for name, pattern in (
            ("datetime", rf"[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[T ]{CLOCK}{ZONE}"),
            ("date", r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
            ("time", rf"{CLOCK}{ZONE}"),
            ("money", r"\$[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?|\$[0-9]+(?:\.[0-9]{2})?"),
            ("minutes", r"[0-9]+ ?min(?:ute)?s? [0-9]+(?:\.[0-9]+)? ?s(?:ec(?:ond)?s?)?\b"),  # 5 min 23 s
            (
                "duration",
                r"[0-9]+(?:\.[0-9]+)?(?: |-)?(?:ms|milliseconds?|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?)\b",
            ),
            ("percent", r"[0-9]+(?:\.[0-9]+)? ?(?:%|percent\b)"),
            # A word of letters and digits: r-e16d, evt_000251, pay_000230, p95, 5xx, v2, claude-opus-5-5.
            ("id", r"[A-Za-z0-9_-]*(?:[A-Za-z_][A-Za-z0-9_-]*[0-9]|[0-9][A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]*"),
            ("number", r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?"),
        )
    )
)
# Numbers in words, which digits would otherwise let past the check. "one", "once", "twice" and the ordinals are not
# read: a stated limit, since prose uses them for much else.
WORDS = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen"
    r"|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundreds?|thousands?|millions?"
    r"|billions?|dozens?|half)\b",
    re.IGNORECASE,
)
SCALE = {"ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001, "min": 60, "mins": 60, "minute": 60}
SCALE |= {"minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
KINDS = {
    "number": "a number",
    "money": "an amount of money",
    "time": "a time",
    "date": "a date",
    "duration": "a duration",
    "percent": "a percentage",
    "id": "an id",
    "word": "a number in words",
}


@dataclass(frozen=True)
class Number:
    """One number in a text: its kind, its value as the check compares it, and where it is, as written."""

    kind: str
    value: object
    said: str
    start: int
    end: int


def _clock(text: str) -> str:
    """A time as HH:MM:SS, or HH:MM when written to the minute: no zone, and no part of a second."""
    hours, minutes, seconds = re.match(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?", text).groups()
    return f"{int(hours):02d}:{minutes}" + (f":{seconds}" if seconds else "")


def _values(kind: str, said: str) -> list[tuple[str, object]]:
    """What a token of this kind stands for, as the check compares it; a time and a date for a datetime."""
    if kind == "datetime":
        return [("date", said[:10]), ("time", _clock(said[11:]))]
    if kind in ("date", "time"):
        return [(kind, said if kind == "date" else _clock(said))]
    if kind == "money":
        dollars, _, cents = said[1:].replace(",", "").partition(".")
        return [("money", int(dollars) * 100 + int(cents or 0))]
    if kind == "minutes":
        minutes, seconds = re.match(r"([0-9]+) ?min\w* ([0-9.]+)", said).groups()
        return [("duration", round(int(minutes) * 60 + float(seconds), 3))]
    if kind == "duration":
        amount, unit = re.match(r"([0-9.]+)[ -]?([a-z]+)", said.lower()).groups()
        return [("duration", round(float(amount) * SCALE.get(unit, 1), 3))]
    if kind == "percent":
        return [("percent", float(re.match(r"[0-9.]+", said).group()))]
    if kind == "id":
        return [("id", said.lower())]
    return [("number", float(said.replace(",", "")))]


def numbers_in(text: str) -> list[Number]:
    """Every number in a text, in order: each run of digits is read as part of exactly one token, and a number written
    in words is one too."""
    found = [
        Number(kind, value, match.group(), match.start(), match.end())
        for match in TOKENS.finditer(text)
        for kind, value in _values(match.lastgroup, match.group())
    ]
    found += [Number("word", m.group().lower(), m.group(), m.start(), m.end()) for m in WORDS.finditer(text)]
    return sorted(found, key=lambda number: number.start)
