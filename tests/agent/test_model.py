"""The model interface, offline: conversations, prompt hashes, the scripted mock, the recording and the replay.

Every reply here is scripted or made up for the test; none came from a model.
"""

import builtins
import json
from dataclasses import replace

import pytest

from klaxon.agent import model as m
from klaxon.agent.model import Message, Recording, ReplayModel, Reply, ScriptedModel, Usage

SYSTEM = "You are a test."
ASK = [Message("user", "Reply with the word ok")]


def conversation(*texts):
    return [Message(m.ROLES[i % 2], text) for i, text in enumerate(texts)]


def made_up_reply(system=SYSTEM, messages=ASK, text="ok", model="claude-opus-5-5"):
    """A reply as a live adapter would return it; made up for these tests."""
    return Reply(
        text=text,
        model=model,
        usage=Usage(input_tokens=12, output_tokens=4, cache_read_tokens=3000, cache_write_tokens=0),
        seconds=2.5,
        source="claude-code",
        prompt_sha256=m.prompt_sha256(system, messages),
        received_at="2026-10-15T17:00:00.000Z",
        cost_usd=0.0123,
        raw=[{"type": "result", "result": text}],
    )


# Conversations


@pytest.mark.parametrize("role", ["system", "tool", "", "User"])
def test_a_turn_is_the_users_or_the_assistants(role):
    with pytest.raises(ValueError):
        Message(role, "hello")


def test_a_turns_content_is_text():
    with pytest.raises(TypeError):
        Message("user", {"tool": "x"})


@pytest.mark.parametrize(
    "turns, problem",
    [
        ([], "empty"),
        (conversation("a", "b"), "end with the user"),
        ([Message("assistant", "hi"), Message("user", "x")], "turn 1 should be the user's"),
        ([Message("user", "a"), Message("user", "b")], "turn 2 should be the assistant's"),
    ],
)
def test_a_conversation_starts_and_ends_with_the_user_and_alternates(turns, problem):
    with pytest.raises(ValueError, match=problem):
        m.check_conversation(turns)


def test_each_turn_must_be_a_message():
    with pytest.raises(TypeError):
        m.check_conversation([{"role": "user", "content": "x"}])


# Prompt hashes


def test_the_prompt_hash_is_stable_so_recordings_stay_matchable():
    # Pinned: a change to how prompts are hashed would orphan every recording made before it. The value is the
    # SHA-256 of {"system":"You are a test.","messages":[["user","Reply with the word ok"]]}, as compact JSON.
    assert m.prompt_sha256(SYSTEM, ASK) == "0237abed982f58ee03b484f79d5dd856f19dc5d1ae39a6cc36de744a520d1751"


@pytest.mark.parametrize(
    "system, turns",
    [
        ("You are a test!", ASK),
        (SYSTEM, [Message("user", "Reply with the word OK")]),
        (SYSTEM, conversation("Reply with the word ok", "ok", "again")),
        (SYSTEM, conversation("again", "ok", "Reply with the word ok")),
    ],
)
def test_any_change_to_the_prompt_changes_its_hash(system, turns):
    assert m.prompt_sha256(system, turns) != m.prompt_sha256(SYSTEM, ASK)


def test_the_role_is_part_of_the_hash():
    a = m.prompt_sha256(SYSTEM, conversation("x", "y", "z"))
    b = m.prompt_sha256(SYSTEM, [Message("user", "x"), Message("user", "y"), Message("user", "z")])
    assert a != b


def test_a_lone_surrogate_hashes_instead_of_crashing():
    # JSON allows "\ud800", so a reply can carry one.
    turns = [Message("user", json.loads('"bad \\ud800 text"'))]
    assert len(m.prompt_sha256(SYSTEM, turns)) == 64
    assert len(m.text_sha256(turns[0].content)) == 64


# Usage


def test_usage_keeps_whole_non_negative_counts_only():
    usage = Usage.from_api(
        {"input_tokens": 12, "output_tokens": -1, "cache_read_input_tokens": True, "cache_creation_input_tokens": "9"}
    )
    assert usage == Usage(input_tokens=12)
    assert Usage.from_api("not usage") == Usage()


def test_total_input_counts_what_the_cache_served_too():
    assert Usage(input_tokens=12, cache_read_tokens=3000, cache_write_tokens=5).total_input_tokens == 3017
    assert Usage().total_input_tokens is None


# The scripted mock


def test_scripted_replies_come_in_order_and_every_call_is_kept():
    model = ScriptedModel(["first", "second"])
    one = model.ask(SYSTEM, ASK)
    two = model.ask(SYSTEM, conversation("Reply with the word ok", "first", "go on"))
    assert (one.text, two.text) == ("first", "second")
    assert one.source == "scripted" and one.model == "scripted" and one.usage == Usage()
    assert one.prompt_sha256 == m.prompt_sha256(SYSTEM, ASK)
    assert [len(turns) for _, turns in model.calls] == [1, 3]
    assert model.calls[1][1][-1].content == "go on"


def test_a_scripted_exception_is_raised_and_the_call_still_kept():
    model = ScriptedModel([m.ModelTimeout("no reply within 300 s")])
    with pytest.raises(m.ModelTimeout):
        model.ask(SYSTEM, ASK)
    assert len(model.calls) == 1


def test_a_scripted_function_sees_the_conversation():
    model = ScriptedModel([lambda system, turns: f"{len(turns)} turn(s), last: {turns[-1].content}"])
    assert model.ask(SYSTEM, ASK).text == "1 turn(s), last: Reply with the word ok"


def test_a_script_that_runs_out_says_so():
    model = ScriptedModel(["only one"])
    model.ask(SYSTEM, ASK)
    with pytest.raises(m.ScriptExhausted, match="1 replies, and this is call 2"):
        model.ask(SYSTEM, ASK)


def test_a_scripted_reply_must_be_text():
    with pytest.raises(TypeError):
        ScriptedModel([lambda system, turns: 42]).ask(SYSTEM, ASK)


def test_the_mock_checks_the_conversation_like_a_live_adapter():
    with pytest.raises(ValueError):
        ScriptedModel(["x"]).ask(SYSTEM, conversation("a", "b"))


# The recording


def test_a_recording_starts_with_a_label_and_where_the_calls_came_from(tmp_path):
    Recording(tmp_path / "calls.jsonl", header={"rerun_command": "make claude-check"})
    (header,) = m.read_records(tmp_path / "calls.jsonl")
    assert header["kind"] == "header" and header["format"] == 1
    assert "received from Claude" in header["label"] and "Not a simulation" in header["label"]
    assert "rate-limit events (the seat's usage state) are left out" in header["label"]
    assert header["rerun_command"] == "make claude-check"


def test_a_response_is_saved_with_its_date_model_prompt_hash_text_and_usage(tmp_path):
    recording = Recording(tmp_path / "calls.jsonl")
    reply = made_up_reply()
    recording.response(SYSTEM, ASK, reply, {"requested_model": "claude-opus-5-5"})
    response = m.read_records(tmp_path / "calls.jsonl")[-1]
    assert response["kind"] == "response" and response["label"] == "recorded response, as received"
    assert response["received_at"] == "2026-10-15T17:00:00.000Z"
    assert response["model"] == "claude-opus-5-5"
    assert response["prompt_sha256"] == m.prompt_sha256(SYSTEM, ASK)
    assert response["text"] == "ok"
    assert response["usage"] == {
        "input_tokens": 12,
        "output_tokens": 4,
        "cache_read_tokens": 3000,
        "cache_write_tokens": 0,
    }
    assert response["accepted"] is True and "refused" not in response
    assert response["raw"] == [{"type": "result", "result": "ok"}]
    assert response["details"] == {"requested_model": "claude-opus-5-5"}


def test_a_refused_response_is_saved_too_with_why(tmp_path):
    recording = Recording(tmp_path / "calls.jsonl")
    refused = m.ModelMismatch("asked for claude-opus-5-5, but the reply says claude-sonnet-5-5 wrote it")
    recording.response(SYSTEM, ASK, made_up_reply(model="claude-sonnet-5-5"), {}, refused)
    response = m.read_records(tmp_path / "calls.jsonl")[-1]
    assert response["accepted"] is False and response["error"] == "ModelMismatch"
    assert response["refused"].startswith("asked for claude-opus-5-5")


def test_the_prompt_can_be_rebuilt_exactly_from_the_recording(tmp_path):
    recording = Recording(tmp_path / "calls.jsonl")
    turns = conversation("alert: lag on the ledger", '{"tool": "query_metrics"}', "result: 193")
    recording.response(SYSTEM, turns, made_up_reply(messages=turns), {})
    records = m.read_records(tmp_path / "calls.jsonl")
    assert m.rebuild_prompt(records, records[-1]) == (SYSTEM, tuple(turns))


def test_each_part_of_a_prompt_is_stored_once(tmp_path):
    path = tmp_path / "calls.jsonl"
    recording = Recording(path)
    first = conversation("alert")
    second = conversation("alert", "look at the lag", "lag is 193")
    recording.response(SYSTEM, first, made_up_reply(messages=first), {})
    recording.response(SYSTEM, second, made_up_reply(messages=second), {})
    Recording(path).response(SYSTEM, second, made_up_reply(messages=second), {})  # reopened: still not repeated
    records = m.read_records(path)
    parts = [r["content"] for r in records if r["kind"] == "prompt_part"]
    assert parts == [SYSTEM, "alert", "look at the lag", "lag is 193"]
    assert [r["kind"] for r in records].count("header") == 2  # one each time the file was opened
    for response in [r for r in records if r["kind"] == "response"]:
        system, turns = m.rebuild_prompt(records, response)
        assert m.prompt_sha256(system, turns) == response["prompt_sha256"]


@pytest.mark.parametrize("source", ["scripted", "replay"])
def test_a_recording_keeps_live_replies_only(tmp_path, source):
    reply = made_up_reply()
    with pytest.raises(ValueError, match="live replies only"):
        Recording(tmp_path / "calls.jsonl").response(SYSTEM, ASK, replace(reply, source=source), {})


def test_a_write_that_fails_leaves_no_half_record_and_stores_no_part(tmp_path, monkeypatch):
    path = tmp_path / "calls.jsonl"
    recording = Recording(path)
    before = path.read_bytes()

    class FullDisk:
        """A file that takes half of what is written, then fails, as a full disk does."""

        def __init__(self, real):
            self.real = real

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.real.close()

        def seek(self, *args):
            return self.real.seek(*args)

        def write(self, data):
            self.real.write(data[: len(data) // 2])
            self.real.flush()
            raise OSError("No space left on device")

        def flush(self):
            self.real.flush()

        def truncate(self, size):
            return self.real.truncate(size)

    monkeypatch.setattr(m, "open", lambda file, mode, **kw: FullDisk(builtins.open(file, mode, **kw)), raising=False)
    with pytest.raises(OSError, match="No space"):
        recording.response(SYSTEM, ASK, made_up_reply(), {})
    monkeypatch.undo()
    assert path.read_bytes() == before  # cut back: the next record does not land on half a line
    recording.response(SYSTEM, ASK, made_up_reply(), {})  # the disk has room again
    records = m.read_records(path)
    assert m.rebuild_prompt(records, records[-1]) == (SYSTEM, tuple(ASK))  # its parts were written this time


def test_a_failed_call_can_rebuild_its_prompt_too(tmp_path):
    Recording(tmp_path / "calls.jsonl").failure(SYSTEM, ASK, m.ModelTimeout("no reply"), 300.0, {}, [])
    records = m.read_records(tmp_path / "calls.jsonl")
    assert m.rebuild_prompt(records, records[-1]) == (SYSTEM, tuple(ASK))


def test_a_failure_is_saved_with_why(tmp_path):
    recording = Recording(tmp_path / "calls.jsonl")
    recording.failure(SYSTEM, ASK, m.ModelTimeout("no reply within 300 s"), 300.1, {"exit_code": -9}, [])
    failure = m.read_records(tmp_path / "calls.jsonl")[-1]
    assert failure["kind"] == "failure" and failure["error"] == "ModelTimeout"
    assert failure["reason"] == "no reply within 300 s" and failure["seconds"] == 300.1


def test_every_record_is_one_ascii_line_whatever_the_text(tmp_path):
    path = tmp_path / "calls.jsonl"
    text = "line separator, paragraph separator, café, \U0001f600"
    turns = [Message("user", text)]
    Recording(path).response(SYSTEM, turns, made_up_reply(messages=turns, text=text), {})
    raw = path.read_bytes()
    assert raw.isascii()
    assert len(raw.decode().splitlines()) == 4  # header, the two parts, the response
    assert m.read_records(path)[-1]["text"] == text


def test_a_tampered_recording_cannot_rebuild_its_prompt(tmp_path):
    path = tmp_path / "calls.jsonl"
    Recording(path).response(SYSTEM, ASK, made_up_reply(), {})
    path.write_text(path.read_text().replace("Reply with the word ok", "Reply with the word yes"))
    records = m.read_records(path)
    with pytest.raises(ValueError, match="does not match"):
        m.rebuild_prompt(records, records[-1])


def test_a_file_that_is_not_records_is_refused(tmp_path):
    path = tmp_path / "calls.jsonl"
    path.write_text('{"kind": "header"}\n[1, 2]\n')
    with pytest.raises(ValueError, match="line 2"):
        m.read_records(path)
    path.write_text("not json\n")
    with pytest.raises(ValueError, match="line 1"):
        m.read_records(path)


# The replay


def recorded(tmp_path, *calls):
    """A recording of made-up responses: (messages, text, refused) for each call."""
    path = tmp_path / "calls.jsonl"
    recording = Recording(path)
    for turns, text, refused in calls:
        error = m.ModelMismatch(refused) if refused else None
        recording.response(SYSTEM, turns, made_up_reply(messages=turns, text=text), {}, error)
    return path


def test_a_replay_serves_the_recorded_reply_for_the_same_prompt_labeled_as_a_replay(tmp_path):
    replay = ReplayModel(recorded(tmp_path, (ASK, "ok", None)))
    reply = replay.ask(SYSTEM, ASK)
    assert reply.text == "ok" and reply.source == "replay"
    assert reply.model == "claude-opus-5-5"  # the model that wrote it, as recorded
    assert reply.received_at == "2026-10-15T17:00:00.000Z"  # when it was first received, not now
    assert reply.usage.cache_read_tokens == 3000 and reply.cost_usd == 0.0123
    assert reply.raw == {"replayed_from": "calls.jsonl", "recorded_from": "claude-code"}
    assert (replay.served, replay.unused) == (1, 0)


def test_a_replay_refuses_a_prompt_that_changed(tmp_path):
    replay = ReplayModel(recorded(tmp_path, (ASK, "ok", None)))
    with pytest.raises(m.NotRecorded, match="the prompt changed"):
        replay.ask(SYSTEM, [Message("user", "Reply with the word yes")])
    with pytest.raises(m.NotRecorded):
        replay.ask("Another system prompt.", ASK)


def test_a_prompt_asked_twice_gets_its_recorded_replies_in_order_then_none(tmp_path):
    replay = ReplayModel(recorded(tmp_path, (ASK, "first", None), (ASK, "second", None)))
    assert [replay.ask(SYSTEM, ASK).text for _ in range(2)] == ["first", "second"]
    with pytest.raises(m.NotRecorded):
        replay.ask(SYSTEM, ASK)


def test_a_replay_gives_back_refusals_and_failures_as_the_errors_they_were_in_order(tmp_path):
    path = recorded(tmp_path, (ASK, "from another model", "asked for claude-opus-5-5"))
    Recording(path).failure(SYSTEM, ASK, m.ModelTimeout("no reply within 300 s"), 300.0, {}, [])
    Recording(path).response(SYSTEM, ASK, made_up_reply(), {})
    replay = ReplayModel(path)
    assert replay.unused == 3
    with pytest.raises(m.ModelMismatch, match="replayed from calls.jsonl: asked for claude-opus-5-5"):
        replay.ask(SYSTEM, ASK)
    with pytest.raises(m.ModelTimeout, match="no reply within 300 s"):  # so a run that retried, retries again
        replay.ask(SYSTEM, ASK)
    assert replay.ask(SYSTEM, ASK).text == "ok"
    with pytest.raises(m.NotRecorded):
        replay.ask(SYSTEM, ASK)


def test_a_replay_refuses_a_tampered_recording_before_serving_anything(tmp_path):
    path = recorded(tmp_path, (ASK, "ok", None))
    path.write_text(path.read_text().replace("You are a test.", "You are approved."))
    with pytest.raises(ValueError, match="does not match"):
        ReplayModel(path)


@pytest.mark.parametrize(
    "edit",
    [
        ('"seconds":2.5', '"seconds":NaN'),
        ('"seconds":2.5', '"seconds":"2.5"'),
        ('"text":"ok"', '"text":null'),
        ('"model":"claude-opus-5-5"', '"model":7'),
    ],
)
def test_a_replay_refuses_a_response_that_lost_its_text_model_or_time(tmp_path, edit):
    path = recorded(tmp_path, (ASK, "ok", None))
    text = path.read_text()
    assert text.count(edit[0]) == 1
    path.write_text(text.replace(*edit))
    with pytest.raises(ValueError, match="lacks its text, model, date or time"):
        ReplayModel(path)


def test_a_replayed_cost_that_is_not_a_number_is_dropped(tmp_path):
    path = recorded(tmp_path, (ASK, "ok", None))
    path.write_text(path.read_text().replace('"cost_usd":0.0123', '"cost_usd":Infinity'))
    assert ReplayModel(path).ask(SYSTEM, ASK).cost_usd is None


@pytest.mark.parametrize(
    "value, number", [(2, 2.0), (0.5, 0.5), (-1, None), (float("nan"), None), (True, None), ("3", None)]
)
def test_finite_keeps_non_negative_finite_numbers_only(value, number):
    assert m.finite(value) == number
