"""The numbers in a text, each with its kind (klaxon.agent.numbers): what a postmortem's check compares with its
facts. Every text here is made up for the tests."""

import re

import pytest

from klaxon.agent.numbers import KINDS, as_number, numbers_in


def read(text):
    return [(number.kind, number.value) for number in numbers_in(text)]


@pytest.mark.parametrize(
    "text, found",
    [
        ("$1,531.87", [("money", 153_187)]),
        ("$493.23 ahead", [("money", 49_323)]),
        ("$40", [("money", 4_000)]),
        ("$4,000", [("money", 400_000)]),
        ("2026-10-21", [("date", "2026-10-21")]),
        ("2026-10-21T09:09:11Z", [("date", "2026-10-21"), ("time", "09:09:11")]),
        ("2026-10-21T09:06:48.445Z", [("date", "2026-10-21"), ("time", "09:06:48")]),
        ("2026-10-21 09:09:11 UTC", [("date", "2026-10-21"), ("time", "09:09:11")]),
        ("at 09:09:11 UTC", [("time", "09:09:11")]),
        ("at 09:09:11Z", [("time", "09:09:11")]),
        ("at 9:09", [("time", "09:09")]),
        ("96 s", [("duration", 96.0)]),
        ("45.8 s", [("duration", 45.8)]),
        ("45.8s", [("duration", 45.8)]),
        ("15 seconds", [("duration", 15.0)]),
        ("1 second", [("duration", 1.0)]),
        ("10 minutes", [("duration", 600.0)]),
        ("a 10-minute wait", [("duration", 600.0)]),
        ("1 min", [("duration", 60.0)]),
        ("200 ms", [("duration", 0.2)]),
        ("2 hours", [("duration", 7200.0)]),
        ("5 min 23 s", [("duration", 323.0)]),
        ("5 minutes 23 seconds", [("duration", 323.0)]),
        ("323 s (5 min 23 s)", [("duration", 323.0), ("duration", 323.0)]),
        ("92%", [("percent", 92.0)]),
        ("4.2 percent", [("percent", 4.2)]),
        ("r-e16d", [("id", "r-e16d")]),
        ("evt_000251's postings", [("id", "evt_000251")]),
        ("pay_000230", [("id", "pay_000230")]),
        ("the p95 latency and the 5xx share", [("id", "p95"), ("id", "5xx")]),
        ("WEBHOOK_SECRET_VERSION v2", [("id", "v2")]),
        ("claude-opus-5-5", [("id", "claude-opus-5-5")]),
        ("100x", [("id", "100x")]),
        ("1,080 payments", [("number", "1080")]),
        ("confidence 0.85.", [("number", "0.85")]),
        ("7 events", [("number", "7")]),
        ("3 steps", [("number", "3")]),
        ("5 services", [("number", "5")]),
        # The review's strings (week 4, item 5): each one token, as written, with its sign.
        ("In all, 4 000 payments", [("number", "4000")]),
        ("4\u202f000 and 4'000", [("number", "4000"), ("number", "4000")]),
        ("4.000 is not 4", [("number", "4.000"), ("number", "4")]),
        ("1/2 of them, a 10^2 rise, 3*4", [("number", "1/2"), ("number", "10^2"), ("number", "3*4")]),
        ("a change of -28 events, +28", [("number", "-28"), ("number", "28")]),
        ("-$1,531.87", [("money", -153_187)]),
        ("$1,531.875", [("money", 153_100), ("number", "875")]),
        ("t=-5 s", [("duration", -5.0)]),
        ("2026-10-21T09:05:13+00:00", [("date", "2026-10-21"), ("time", "09:05:13")]),
        ("1 minute and 36 seconds", [("duration", 96.0)]),
        ("a 2-round wait", [("number", "2")]),
        ("deploy 21-22", [("number", "21"), ("number", "22")]),
        ("zero, tens and tenfold", [("word", "zero"), ("word", "tens"), ("word", "tenfold")]),
        # The re-check's strings: an offset is a zone only after a whole datetime, and only +00:00.
        ("It ran 09:06-23:59 UTC.", [("time", "09:06"), ("time", "23:59")]),
        ("From 09:07:35-09:09:11", [("time", "09:07:35"), ("time", "09:09:11")]),
        ("at 09:06:47+05:30", [("time", "09:06:47"), ("time", "05:30")]),
        ("2026-10-21T09:05:13+05:30", [("date", "2026-10-21"), ("time", "09:05:13"), ("time", "05:30")]),
        ("a 28-fold rise", [("number", "28-fold")]),
        ("two rounds, ten refunds, a hundred times, half", [("word", w) for w in ("two", "ten", "hundred", "half")]),
        ("Two", [("word", "two")]),
    ],
)
def test_each_number_is_read_with_its_kind_and_value(text, found):
    assert read(text) == found


@pytest.mark.parametrize("text", ["one service", "once", "twice", "the first check", "a second round", "double"])
def test_one_once_twice_and_the_ordinals_are_not_read(text):
    """A stated limit: prose uses them for much else."""
    assert read(text) == []


TRICKY = [
    "At 09:09:11 UTC on 2026-10-21, 28 events (evt_000251, evt_000261) were counted twice: $1,531.87, 92%, 5 min 23 s.",
    "p95 7.3 s; 5xx share 9.6%; 1,080 payments; r-742c; 10-minute; 3.14.15; 1/2; #5; -5; 2.5 to 3 minutes; 100x",
    "2026-10-21T090513Z_gated-run_duplicate-delivery and t=76.3 s, 0.0 s, 99:99 and 2026-13-45",
    "v1,v2 and LEDGER_HANDLER=naive and 1st, 2nd; 12,34,567 and 1,2,3; $1,23 and $.5",
    "4 000 and 4\u202f000; 1/2 and 10^2; -28 and -$1,531.87 and $1,531.875; 09:05:13+00:00; 2-round; 1 minute and 36 s",
]


@pytest.mark.parametrize("text", TRICKY)
def test_every_digit_is_read_as_part_of_exactly_one_token(text):
    """No digit can pass the check unread: each one sits inside a token, and tokens do not overlap (a datetime is
    one span read as two values)."""
    spans = sorted({(n.start, n.end) for n in numbers_in(text) if n.kind != "word"})
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert end <= start
    covered = set()
    for start, end in spans:
        covered.update(range(start, end))
    assert {i for i, ch in enumerate(text) if ch in "0123456789"} <= covered


def test_the_same_value_of_another_kind_is_another_number():
    """$28.00 is not 28, 96 s is not 96, 92% is not 92: the check compares kinds too."""
    kinds = {(kind, value) for text in ("$28.00", "28", "96 s", "96", "92%", "92") for kind, value in read(text)}
    assert kinds == {
        ("money", 2_800),
        ("number", "28"),
        ("duration", 96.0),
        ("number", "96"),
        ("percent", 92.0),
        ("number", "92"),
    }


def test_a_rounded_amount_is_another_amount():
    assert read("$1,532") != read("$1,531.87")


def test_a_number_from_json_is_written_as_python_writes_it():
    assert [as_number(value) for value in (7, 249.0, 0.85, 45.822, -5)] == [
        ("number", "7"),
        ("number", "249"),
        ("number", "0.85"),
        ("number", "45.822"),
        ("number", "-5"),
    ]


def test_digits_other_than_0_to_9_are_not_read_as_numbers():
    """They are the postmortem's style check's to refuse (klaxon.agent.postmortem): a number code cannot read."""
    for text in ("\u0663", "\uff11\uff12", "\u00bd", "\u2167", "\u4e94"):
        assert read(text) == []


def test_each_kind_has_a_name_for_the_problems_it_raises():
    kinds = {kind for text in TRICKY for kind, _ in read(text)} | {"word"}
    assert kinds <= set(KINDS)
    assert all(re.fullmatch(r"an? [a-z ]+", name) for name in KINDS.values())
