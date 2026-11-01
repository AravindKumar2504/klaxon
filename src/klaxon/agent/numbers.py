"""The numbers in a text, each with its kind, so that a narrative's numbers can be checked against its facts (week 4,
item 5, klaxon.agent.postmortem).

Each run of the digits 0 to 9 is read as part of exactly one token, and each token as one of these kinds:

    datetime  2026-10-21T09:06:47Z, 2026-10-21 09:06:47 UTC: read as a date and a time
    date      2026-10-21
    time      09:06:47, 09:06:47 UTC, 9:06: to the second, or to the minute; its zone (Z, UTC, +00:00) is no number
    money     $1,531.87, $40, -$5.00: in cents, with its sign
    duration  96 s, 45.8 s, 10 minutes, a 10-minute wait, 200 ms, 5 min 23 s, 1 minute and 36 seconds: in seconds
    percent   92%, 4.2 percent
    id        r-e16d, evt_000251, pay_000230, p95, 5xx, v2, claude-opus-5-5: a word of letters and digits
    number    1,080, 4 000, 0.85, -28, and 1/2 or 10^2 as one token: compared as written, with its sign and without
              its thousands separators, so 4.000 is not 4

A number written in words (two, ten, hundred, half, zero, tenfold) is a token too. "One", "once", "twice" and the
ordinals are not read: prose uses them for much else, so they are a stated limit of the check.

Two tokens are the same number when they are of the same kind and stand for the same value: $28.00 is not 28, 96 s is
not 96, and $1,532 is not $1,531.87.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SIGN = r"(?:(?<![A-Za-z0-9_])[-+])?"  # a minus or a plus that starts a number, not a hyphen inside a word
CLOCK = r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
ZONE = r"(?:Z|[ ]?UTC\b|[+-][0-9]{2}:[0-9]{2})?"
GROUP = "[ '\u00a0\u2000-\u200a\u2019\u202f\u205f\u3000]"  # a space or an apostrophe between thousands: 4 000, 4'000
TOKENS = re.compile(
    "|".join(
        f"(?P<{name}>{pattern})"
        for name, pattern in (
            ("datetime", rf"[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[T ]{CLOCK}{ZONE}"),
            ("date", r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
            ("time", rf"{CLOCK}{ZONE}"),
            ("money", rf"{SIGN}\$(?:[0-9]{{1,3}}(?:,[0-9]{{3}})+|[0-9]+)(?:\.[0-9]{{2}})?(?![0-9])"),
            ("grouped", rf"{SIGN}[0-9]{{1,3}}(?:{GROUP}[0-9]{{3}})+(?![0-9])"),
            ("ratio", rf"{SIGN}[0-9]+(?:\.[0-9]+)?(?:[/^*\u00d7\u00f7][0-9]+(?:\.[0-9]+)?)+"),  # 1/2, 10^2, 3*4
            ("minutes", rf"{SIGN}[0-9]+ ?min(?:ute)?s?,?(?: and)? [0-9]+(?:\.[0-9]+)? ?s(?:ec(?:ond)?s?)?\b"),
            (
                "duration",
                rf"{SIGN}[0-9]+(?:\.[0-9]+)?(?: |-)?(?:ms|milliseconds?|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?)\b",
            ),
            ("percent", rf"{SIGN}[0-9]+(?:\.[0-9]+)? ?(?:%|percent\b)"),
            ("before_a_word", r"[0-9]+(?:\.[0-9]+)?(?=-[A-Za-z])"),  # 2-round: a number before a hyphen and a word
            # A word of letters and digits: r-e16d, evt_000251, pay_000230, p95, 5xx, v2, claude-opus-5-5.
            ("id", r"[A-Za-z0-9_-]*(?:[A-Za-z_][A-Za-z0-9_-]*[0-9]|[0-9][A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]*"),
            ("number", rf"{SIGN}[0-9]+(?:,[0-9]{{3}})*(?:\.[0-9]+)?"),
        )
    )
)
# Numbers in words, which digits would otherwise let past the check. "one", "once", "twice" and the ordinals are not
# read: a stated limit, since prose uses them for much else.
WORDS = re.compile(
    r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|tens|eleven|twelve|thirteen|fourteen|fifteen|sixteen"
    r"|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundreds?|thousands?"
    r"|millions?|billions?|dozens?|half|twofold|threefold|fourfold|fivefold|tenfold|hundredfold|thousandfold)\b",
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


def as_number(value: int | float) -> tuple[str, str]:
    """A number from JSON as the check compares it: written as Python writes it, and a whole float as a whole number
    (249.0 is 249)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return ("number", str(value) if isinstance(value, int) else repr(value))


def _values(kind: str, said: str) -> list[tuple[str, object]]:
    """What a token of this kind stands for, as the check compares it; a date and a time for a datetime."""
    if kind == "datetime":
        return [("date", said[:10]), ("time", _clock(said[11:]))]
    if kind in ("date", "time"):
        return [(kind, said if kind == "date" else _clock(said))]
    sign = -1 if said[:1] == "-" else 1
    body = said.lstrip("+-")
    if kind == "money":
        dollars, _, cents = body[1:].replace(",", "").partition(".")
        return [("money", sign * (int(dollars) * 100 + int(cents or 0)))]
    if kind == "minutes":
        minutes, seconds = re.match(r"([0-9]+) ?min\w*,?(?: and)? ([0-9.]+)", body).groups()
        return [("duration", sign * round(int(minutes) * 60 + float(seconds), 3))]
    if kind == "duration":
        amount, unit = re.match(r"([0-9.]+)[ -]?([a-z]+)", body.lower()).groups()
        return [("duration", sign * round(float(amount) * SCALE.get(unit, 1), 3))]
    if kind == "percent":
        return [("percent", sign * float(re.match(r"[0-9.]+", body).group()))]
    if kind == "id":
        return [("id", said.lower())]
    written = re.sub(f"{GROUP}|,", "", body)  # a number, as written: no thousands separators, its sign kept
    return [("number", ("-" if sign < 0 else "") + written)]


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
