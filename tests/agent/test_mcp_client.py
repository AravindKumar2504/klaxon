"""Klaxon's MCP client, offline: the handshake, tools/list and tools/call against Klaxon's real server, a fake
server that misbehaves on purpose, and the recording and replay of tool results.

The real server runs with the stack out of reach or unused: these tests call only tools that fail before they
read anything. Every tool result here is made up for the test or such an error.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from klaxon.agent import mcp_client as mc
from klaxon.agent import mcp_server
from klaxon.agent.model import read_records
from klaxon.agent.tools import Tool
from tests.agent.inprocess import InProcess

FAKE = Path(__file__).with_name("fake_mcp_server.py")
SCHEMA = {"type": "object", "properties": {"say": {"type": "string", "maxLength": 20}}, "additionalProperties": False}


def fake(mode, file=None, **options):
    command = [sys.executable, str(FAKE), mode] + ([str(file)] if file else [])
    return mc.McpClient(command=command, **options)


def in_process(tools):
    transport = InProcess(mcp_server.Server(tools))
    return mc.McpClient(transport=lambda: transport), transport


def echo_tool(name="service_health"):
    return Tool(name, "Echo", "Says it back.", SCHEMA, lambda args: {"said": args.get("say", "")})


# Against Klaxon's real server, as a process


@pytest.fixture(scope="module")
def real():
    client = mc.McpClient()
    specs = client.open()
    yield client, specs
    client.close()


def test_the_real_server_completes_the_handshake_and_lists_eight_read_only_tools(real):
    client, specs = real
    assert [s.name for s in specs] == [
        "list_metrics",
        "query_metrics",
        "search_logs",
        "search_traces",
        "get_trace",
        "service_health",
        "recent_deploys",
        "money_check",
    ]
    assert all(spec.read_only for spec in specs)
    assert client.server_info == {"name": "klaxon-ops", "version": "0.0.1", "protocol": "2025-11-25"}


def test_a_tool_error_from_the_real_server_comes_back_as_a_result_marked_as_one(real):
    client, _ = real
    assert client.call("query_metrics", {"query": ""}, timeout=30) == mc.ToolResult("query must not be empty", True)
    bad = client.call("get_trace", {"trace_id": "not hex"}, timeout=30)
    assert bad == mc.ToolResult("trace_id does not match ^[0-9a-fA-F]{1,32}$", True)


def test_closing_stops_the_server_process():
    client = mc.McpClient()
    client.open()
    proc = client._transport.proc
    client.close()
    assert proc.poll() is not None and proc.returncode == 0  # it saw the end of its input and exited on its own


def test_the_default_command_is_klaxons_own_server():
    assert mc.SERVER_COMMAND == (sys.executable, "-m", "klaxon.agent.mcp_server")


# Against a fake server that misbehaves


def test_a_good_fake_server_works_and_what_was_sent_arrived_intact(tmp_path):
    seen = tmp_path / "seen.jsonl"
    client = fake("good", seen)
    assert [s.name for s in client.open()] == ["service_health"]
    assert client.call("service_health", {"note": "é ☃  "}, timeout=10) == mc.ToolResult('{"services": []}', False)
    client.close()
    assert json.loads(seen.read_text()) == {"name": "service_health", "arguments": {"note": "é ☃  "}}


def test_a_tool_that_does_not_answer_in_time_is_a_timeout_and_the_server_is_stopped():
    client = fake("slow")
    client.open()
    proc = client._transport.proc
    started = time.monotonic()
    with pytest.raises(mc.McpTimeout, match="gave no answer within 0.5 s; the tool server was stopped"):
        client.call("service_health", {}, timeout=0.5)
    assert time.monotonic() - started < 2.0  # the fake answers after 3 s
    assert proc.poll() is not None and client._transport is None
    with pytest.raises(mc.McpError, match="not running"):
        client.call("service_health", {}, timeout=1)


def test_a_timeout_stops_everything_the_server_started(tmp_path):
    client = fake("child", tmp_path / "seen.jsonl")
    client.open()
    with pytest.raises(mc.McpTimeout):
        client.call("service_health", {}, timeout=1.5)
    child = int((tmp_path / "seen.jsonl.child").read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("the server's child outlived the timeout")


def test_a_server_that_never_finishes_the_handshake_is_a_timeout():
    client = fake("silent", start_timeout=0.5)
    started = time.monotonic()
    with pytest.raises(mc.McpTimeout, match="did not start within 0.5 s"):
        client.open()
    assert time.monotonic() - started < 2.0 and client._transport is None


def test_a_server_that_exits_mid_call_is_an_error_with_its_last_words():
    client = fake("exit-on-call")
    client.open()
    with pytest.raises(mc.McpError, match="exited .its log ends: fake server: giving up on purpose"):
        client.call("service_health", {}, timeout=10)


@pytest.mark.parametrize(
    "mode, problem",
    [
        ("garbage-on-call", "a line that is not JSON"),
        ("wrong-id", "answered another request than tools/call"),
        ("true-id", "answered another request than tools/call"),
        ("no-text", "result holds no text"),
        ("error-answer", "refused tools/call: Unknown tool: x .code -32602"),
    ],
)
def test_a_server_that_breaks_the_protocol_ends_the_session(mode, problem):
    client = fake(mode)
    client.open()
    with pytest.raises(mc.McpError, match=problem):
        client.call("service_health", {}, timeout=10)
    assert client._transport is None


@pytest.mark.parametrize(
    "mode, problem",
    [
        ("wrong-version", "speaks MCP 2024-11-05, not 2025-11-25"),
        ("wrong-name", "is not klaxon-ops"),
        ("paged", "lists its tools in pages"),
    ],
)
def test_a_server_that_is_not_klaxons_or_speaks_another_version_is_refused_at_the_handshake(mode, problem):
    client = fake(mode)
    with pytest.raises(mc.McpError, match=problem):
        client.open()
    assert client._transport is None


def test_a_message_over_the_size_limit_ends_the_session(monkeypatch):
    monkeypatch.setattr(mc, "MAX_LINE", 1000)
    client = fake("huge-on-call")
    client.open()
    started = time.monotonic()
    with pytest.raises(mc.McpError, match="over 1,000 bytes"):
        client.call("service_health", {}, timeout=10)
    assert time.monotonic() - started < 2.0


def test_notifications_and_blank_lines_before_the_answer_are_skipped():
    client = fake("notify-first")
    client.open()
    assert client.call("service_health", {}, timeout=10).text == '{"services": []}'
    client.close()


def test_a_server_that_floods_its_log_is_drained_and_never_blocks():
    client = fake("stderr-flood")
    assert [s.name for s in client.open()] == ["service_health"]  # about 1.2 MB of log first
    assert len(client._transport._stderr) <= mc.MAX_STDERR
    client.close()


def test_the_last_words_of_a_server_that_exited_are_read_even_if_nothing_else_was():
    transport = mc.StdioTransport([sys.executable, str(FAKE), "stderr-then-exit"])
    transport.proc.wait(timeout=10)
    assert transport.last_words() == " (its log ends: fake server: cannot go on)"
    transport.close()
    assert transport.last_words() == " (its log ends: fake server: cannot go on)"  # kept, and never read again


def test_a_server_that_cannot_start_is_an_error():
    client = mc.McpClient(command=["/nonexistent/klaxon-server"])
    with pytest.raises(mc.McpError, match="could not be started"):
        client.open()


def test_a_session_opens_once():
    client = fake("good")
    client.open()
    with pytest.raises(mc.McpError, match="already open"):
        client.open()
    client.close()


# What the client sends, against Klaxon's real server code in this process


def test_the_client_sends_the_handshake_then_lists_then_calls_and_nothing_else():
    client, transport = in_process({"service_health": echo_tool()})
    client.open()
    assert client.call("service_health", {"say": "hi"}, timeout=5) == mc.ToolResult('{"said": "hi"}', False)
    methods = [message.get("method") for message in transport.sent]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert transport.sent[0]["params"]["protocolVersion"] == "2025-11-25"
    assert transport.sent[3]["params"] == {"name": "service_health", "arguments": {"say": "hi"}}
    assert [m.get("id") for m in transport.sent] == [1, None, 2, 3]


def test_a_tool_error_in_klaxons_server_code_comes_back_marked():
    client, _ = in_process({"service_health": echo_tool()})
    client.open()
    assert client.call("service_health", {"say": 7}, timeout=5) == mc.ToolResult("say must be a string", True)


def test_an_unknown_tool_is_a_protocol_error_that_ends_the_session():
    client, _ = in_process({"service_health": echo_tool()})
    client.open()
    with pytest.raises(mc.McpError, match="refused tools/call: Unknown tool: restart"):
        client.call("restart", {}, timeout=5)


def test_a_listed_tool_must_have_a_plain_name_and_a_schema_for_an_object():
    for tool, problem in [
        (Tool("Restart Everything", "x", "x", SCHEMA, lambda a: {}), "not a plain name"),
        (Tool("ok", "x", "x", {"type": "string"}, lambda a: {}), "no input schema for an object"),
    ]:
        client, _ = in_process({tool.name: tool})
        with pytest.raises(mc.McpError, match=problem):
            client.open()


def test_read_only_means_both_hints_said_outright():
    spec = lambda **a: mc.ToolSpec("x", "x", "x", {"type": "object"}, a)  # noqa: E731
    assert spec(readOnlyHint=True, destructiveHint=False).read_only
    assert not spec(readOnlyHint=True).read_only  # MCP's default for destructiveHint is true
    assert not spec(destructiveHint=False).read_only
    assert not spec(readOnlyHint="true", destructiveHint=False).read_only
    assert not spec().read_only


# Recording and replaying tool results


class Canned:
    """A tool client with canned answers, made up for the test."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.server_info = {"name": "klaxon-ops", "version": "0", "protocol": "2025-11-25"}
        self.closed = False

    def open(self):
        return [mc.ToolSpec("service_health", "Service health", "Made up.", SCHEMA, {"readOnlyHint": True})]

    def call(self, name, arguments, timeout):
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closed = True


def test_every_result_is_recorded_with_the_tools_as_listed(tmp_path):
    path = tmp_path / "tools.jsonl"
    recorded = mc.RecordedTools(
        Canned(mc.ToolResult('{"a": 1}', False), mc.ToolResult("bad", True)), path, {"run": "r1"}
    )
    recorded.open()
    recorded.call("service_health", {"say": "x"}, 5)
    recorded.call("service_health", {}, 5)
    recorded.close()
    header, first, second = read_records(path)
    assert (
        header["kind"] == "header"
        and header["run"] == "r1"
        and "each written here before the model saw it" in header["label"]
    )
    assert header["tools"][0]["name"] == "service_health" and header["server"]["name"] == "klaxon-ops"
    assert (first["seq"], first["tool"], first["arguments"], first["outcome"]) == (
        1,
        "service_health",
        {"say": "x"},
        "result",
    )
    assert (first["text"], first["is_error"]) == ('{"a": 1}', False)
    assert (second["text"], second["is_error"]) == ("bad", True)
    assert path.read_bytes().isascii()


def test_timeouts_and_failures_are_recorded_too(tmp_path):
    path = tmp_path / "tools.jsonl"
    recorded = mc.RecordedTools(Canned(mc.McpTimeout("no answer"), mc.McpError("exited")), path)
    recorded.open()
    with pytest.raises(mc.McpTimeout):
        recorded.call("service_health", {}, 5)
    with pytest.raises(mc.McpError):
        recorded.call("service_health", {}, 5)
    _, timeout, failed = read_records(path)
    assert (timeout["outcome"], timeout["error"]) == ("timeout", "no answer")
    assert (failed["outcome"], failed["error"]) == ("failed", "exited")


def test_a_result_that_cannot_be_recorded_is_not_passed_on(tmp_path, monkeypatch):
    recorded = mc.RecordedTools(Canned(mc.ToolResult("secret-ish", False)), tmp_path / "tools.jsonl")
    recorded.open()
    monkeypatch.setattr(mc, "_append", lambda path, records: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        recorded.call("service_health", {}, 5)


def test_a_failed_open_is_recorded(tmp_path):
    class Broken(Canned):
        def open(self):
            raise mc.McpError("the tool server could not be started (FileNotFoundError)")

    path = tmp_path / "tools.jsonl"
    with pytest.raises(mc.McpError):
        mc.RecordedTools(Broken(), path).open()
    [header] = read_records(path)
    assert header["failed"].startswith("McpError: the tool server could not be started") and "tools" not in header


def recorded_run(tmp_path, *answers):
    path = tmp_path / "tools.jsonl"
    recorded = mc.RecordedTools(Canned(*answers), path)
    recorded.open()
    for _ in answers:
        try:
            recorded.call("service_health", {"say": "x"}, 5)
        except mc.McpError:
            pass
    return path


def test_a_replay_serves_the_same_results_for_the_same_calls_in_order(tmp_path):
    path = recorded_run(tmp_path, mc.ToolResult("one", False), mc.ToolResult("two", True))
    replay = mc.ReplayTools(path)
    assert [s.name for s in replay.open()] == ["service_health"]
    assert replay.call("service_health", {"say": "x"}, 5) == mc.ToolResult("one", False)
    assert replay.call("service_health", {"say": "x"}, 5) == mc.ToolResult("two", True)
    assert replay.served == 2 and replay.unused == 0
    with pytest.raises(mc.McpError, match="went further than the recorded one"):
        replay.call("service_health", {"say": "x"}, 5)


@pytest.mark.parametrize(
    "name, arguments", [("money_check", {"say": "x"}), ("service_health", {"say": "y"}), ("service_health", {})]
)
def test_a_replay_refuses_a_different_call(tmp_path, name, arguments):
    replay = mc.ReplayTools(recorded_run(tmp_path, mc.ToolResult("one", False)))
    with pytest.raises(mc.McpError, match="another tool call at this point"):
        replay.call(name, arguments, 5)
    assert replay.unused == 1  # nothing was served


def test_in_a_replay_true_is_not_1(tmp_path):
    path = tmp_path / "tools.jsonl"
    recorded = mc.RecordedTools(Canned(mc.ToolResult("one", False)), path)
    recorded.open()
    recorded.call("service_health", {"wait": True}, 5)
    with pytest.raises(mc.McpError, match="another tool call"):
        mc.ReplayTools(path).call("service_health", {"wait": 1}, 5)


def test_a_replay_gives_back_timeouts_and_failures_as_they_were(tmp_path):
    replay = mc.ReplayTools(recorded_run(tmp_path, mc.McpTimeout("slow"), mc.McpError("gone")))
    with pytest.raises(mc.McpTimeout, match="slow"):
        replay.call("service_health", {"say": "x"}, 5)
    with pytest.raises(mc.McpError, match="gone"):
        replay.call("service_health", {"say": "x"}, 5)


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r.pop("text"),
        lambda r: r.update(is_error="no"),
        lambda r: r.update(outcome="maybe"),
        lambda r: r.update(arguments=[]),
        lambda r: r.pop("tool"),
    ],
)
def test_a_replay_refuses_a_damaged_recording_before_serving_anything(tmp_path, edit):
    path = recorded_run(tmp_path, mc.ToolResult("one", False))
    header, record = read_records(path)
    edit(record)
    path.write_text(json.dumps(header) + "\n" + json.dumps(record) + "\n")
    with pytest.raises(ValueError, match="damaged or edited"):
        mc.ReplayTools(path)


def test_a_replay_needs_exactly_one_list_of_tools(tmp_path):
    path = tmp_path / "tools.jsonl"
    path.write_text("")
    with pytest.raises(ValueError, match="one list of tools"):
        mc.ReplayTools(path)
