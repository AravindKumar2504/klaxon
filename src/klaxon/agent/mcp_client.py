"""Klaxon's MCP client, written from scratch: how the loop calls its tools, through Klaxon's own MCP server.

The counterpart of mcp_server.py: JSON-RPC 2.0, one message per line, over the stdin and stdout of a server
process this client starts itself (python -m klaxon.agent.mcp_server), at MCP revision 2025-11-25:

    initialize                  the version this client speaks; the server must answer that one, as klaxon-ops
    notifications/initialized   the handshake is done
    tools/list                  each tool's name, description, input schema and annotations
    tools/call                  one tool with its arguments; the result is text, marked isError or not

Every message from the server is read with a deadline, so a tool that hangs cannot hang the loop: at the
deadline the server's whole process group is killed, and the call fails with McpTimeout. A message over the
size limit or not JSON-RPC, a server that exits, or an answer to another request ends the session with
McpError. The server's stderr (its log) is drained as it comes, so it can never fill up and block the
server; its last lines are kept for error messages.

This client only ever sends the handshake, tools/list, and tools/call. Which tools may be called is the
loop's decision (klaxon.agent.loop), not this client's.

Also here, for the same interface: RecordedTools writes every tool result to a file before the loop sees it;
ReplayTools serves a recorded run's results again, for the same calls in the same order, so that a run can be
replayed offline with klaxon.agent.model.ReplayModel.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from klaxon import __version__
from klaxon.agent.model import read_records, utc_now
from klaxon.agent.tools import clean_text
from klaxon.patient.stack import REPO

PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "klaxon-ops"
SERVER_COMMAND = (sys.executable, "-m", "klaxon.agent.mcp_server")
START_TIMEOUT = 30.0  # seconds for the server to start, finish the handshake and list its tools
CLOSE_TIMEOUT = 5.0  # seconds the server gets to exit on its own once its stdin is closed
MAX_LINE = 4_000_000  # bytes in one message: a result is at most 60,000 characters, sent twice, escaped
MAX_STDERR = 20_000  # bytes of the server's log kept: its last words
MAX_SKIPPED = 100  # messages that are not the answer awaited, before the session is given up
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
FORMAT = 1  # of a tool recording's records


class McpError(RuntimeError):
    """The tool server failed: it could not start, broke the protocol, refused a request, or exited."""


class McpTimeout(McpError):
    """The tool server did not answer in time."""


@dataclass(frozen=True)
class ToolSpec:
    """One tool, as tools/list describes it."""

    name: str
    title: str
    description: str
    input_schema: dict
    annotations: dict = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        """Marked read-only and not destructive, both said outright: MCP's defaults are the opposite."""
        return self.annotations.get("readOnlyHint") is True and self.annotations.get("destructiveHint") is False


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool


class ToolClient(Protocol):
    """What the loop needs from its tools."""

    def open(self, timeout: float = START_TIMEOUT) -> list[ToolSpec]: ...

    def call(self, name: str, arguments: Mapping[str, object], timeout: float) -> ToolResult: ...

    def close(self) -> None: ...


class Transport(Protocol):
    """One line per message, each way."""

    def send(self, line: str) -> None: ...

    def receive(self, deadline: float) -> str | None:
        """The next line, without its newline; None once the server's output has ended. McpTimeout at the
        deadline (time.monotonic())."""
        ...

    def close(self, graceful: bool = True) -> None: ...

    def last_words(self) -> str: ...


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # already gone


class StdioTransport:
    """The server as a child process, in a process group of its own, so that every process it starts (docker,
    for one) is stopped with it."""

    def __init__(
        self, command: Sequence[str] = SERVER_COMMAND, cwd: Path | str | None = REPO, env: Mapping | None = None
    ) -> None:
        try:
            self.proc = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=None if env is None else dict(env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"the tool server could not be started ({type(exc).__name__})") from None
        self._out, self._err = self.proc.stdout.fileno(), self.proc.stderr.fileno()
        self._selector = selectors.DefaultSelector()
        for fd in (self._out, self._err):
            os.set_blocking(fd, False)
            self._selector.register(fd, selectors.EVENT_READ)
        self._pending = b""
        self._ended = False  # the server's stdout reached its end
        self._stderr = b""
        self._closed = False

    def send(self, line: str) -> None:
        try:
            self.proc.stdin.write(line.encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise McpError(f"the tool server stopped reading its input{self.last_words()}") from None

    def receive(self, deadline: float) -> str | None:
        while True:
            if b"\n" in self._pending:
                line, self._pending = self._pending.split(b"\n", 1)
                return self._line(line)
            if len(self._pending) > MAX_LINE:
                raise McpError(f"the tool server sent a message over {MAX_LINE:,} bytes")
            if self._ended:
                line, self._pending = self._pending, b""
                return self._line(line) if line.strip() else None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTimeout("the tool server did not answer in time")
            ready = self._selector.select(min(remaining, 0.25))
            if not ready and self.proc.poll() is not None:
                self._ended = True  # it exited and nothing more came: whatever still holds the pipe is not waited for
            for key, _ in ready:
                try:
                    chunk = os.read(key.fd, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    self._selector.unregister(key.fd)
                    self._ended = self._ended or key.fd == self._out
                elif key.fd == self._err:
                    self._stderr = (self._stderr + chunk)[-MAX_STDERR:]
                else:
                    self._pending += chunk

    @staticmethod
    def _line(line: bytes) -> str:
        if len(line) > MAX_LINE:
            raise McpError(f"the tool server sent a message over {MAX_LINE:,} bytes")
        return line.decode("utf-8", "replace")

    def last_words(self) -> str:
        """The last line of the server's log, for an error message: cleaned and short. What is still in the pipe
        is read first, without waiting: a server that exits can close its output before its log is read."""
        for _ in range(0 if self._closed else 64):
            try:
                chunk = os.read(self._err, 65_536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            self._stderr = (self._stderr + chunk)[-MAX_STDERR:]
        lines = [line.strip() for line in self._stderr.decode("utf-8", "replace").splitlines() if line.strip()]
        return f" (its log ends: {clean_text(lines[-1], 300)})" if lines else ""

    def close(self, graceful: bool = True) -> None:
        """Stop the server: with graceful, close its input and give it CLOSE_TIMEOUT to exit on its own. Then its
        whole process group is killed, on every path, so nothing it started lives on."""
        try:
            if graceful and self.proc.poll() is None:
                try:
                    self.proc.stdin.close()  # its serve loop ends at the end of its input
                except (OSError, ValueError):
                    pass
                try:
                    self.proc.wait(timeout=CLOSE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            self._closed = True
            _kill_group(self.proc)
            try:
                self.proc.wait(timeout=CLOSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass  # killed, but not reaped yet
            self._selector.close()
            for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not a JSON number")


class McpClient:
    """A session with Klaxon's MCP server. open() starts the server and lists its tools, within its timeout;
    call() runs one tool; close() stops the server. A timeout or a broken session closes it: open() again to start
    a new one."""

    def __init__(
        self,
        command: Sequence[str] = SERVER_COMMAND,
        env: Mapping | None = None,
        cwd: Path | str | None = REPO,
        transport: Callable[[], Transport] | None = None,
        start_timeout: float = START_TIMEOUT,
    ) -> None:
        self._make = transport or (lambda: StdioTransport(command, cwd=cwd, env=env))
        self._transport: Transport | None = None
        self._last_id = 0
        self.start_timeout = start_timeout
        self.server_info: dict = {}

    def open(self, timeout: float | None = None) -> list[ToolSpec]:
        if self._transport is not None:
            raise McpError("this session is already open")
        timeout = self.start_timeout if timeout is None else timeout
        self._transport = self._make()
        deadline = time.monotonic() + timeout
        try:
            hello = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "klaxon-loop", "version": __version__},
            }
            self._check_initialize(self._request("initialize", hello, deadline))
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return self._specs(self._request("tools/list", {}, deadline))
        except McpTimeout:
            self.close(graceful=False)
            raise McpTimeout(f"the tool server did not start within {timeout:.1f} s") from None
        except McpError:
            self.close(graceful=False)
            raise

    def call(self, name: str, arguments: Mapping[str, object], timeout: float) -> ToolResult:
        """Run one tool. Its result, or a tool error, comes back as a ToolResult; McpTimeout if no answer came
        within timeout seconds (the server is then stopped), McpError if the session broke."""
        if self._transport is None:
            raise McpError("the tool server is not running")
        deadline = time.monotonic() + timeout
        try:
            return self._tool_result(
                self._request("tools/call", {"name": name, "arguments": dict(arguments)}, deadline)
            )
        except McpTimeout:
            self.close(graceful=False)  # it is busy with the call, and handles one request at a time
            raise McpTimeout(f"{name} gave no answer within {timeout:.1f} s; the tool server was stopped") from None
        except McpError:
            self.close(graceful=False)
            raise

    def close(self, graceful: bool = True) -> None:
        if self._transport is not None:
            transport, self._transport = self._transport, None
            transport.close(graceful)

    def _send(self, message: dict) -> None:
        # ASCII only, as the server writes: no line separator inside a text can split a message.
        self._transport.send(json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False))

    def _request(self, method: str, params: dict, deadline: float) -> dict:
        self._last_id += 1
        request_id = self._last_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        for _ in range(MAX_SKIPPED):
            line = self._transport.receive(deadline)
            if line is None:
                raise McpError(f"the tool server exited{self._transport.last_words()}")
            message = self._message(line)
            if message is None or "method" in message:
                continue  # a blank line or a notification: nothing this client waits for
            if type(message.get("id")) is not int or message["id"] != request_id:  # true is not 1
                raise McpError(f"the tool server answered another request than {method}")
            return self._answer(message, method)
        raise McpError(f"the tool server sent {MAX_SKIPPED} messages that were not the answer to {method}")

    @staticmethod
    def _message(line: str) -> dict | None:
        if not line.strip():
            return None
        try:
            message = json.loads(line, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            raise McpError("the tool server sent a line that is not JSON") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise McpError("the tool server sent a message that is not JSON-RPC 2.0")
        return message

    @staticmethod
    def _answer(message: dict, method: str) -> dict:
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            code, said = error.get("code"), error.get("message")
            raise McpError(
                f"the tool server refused {method}: {clean_text(str(said), 200)} (code {clean_text(str(code), 20)})"
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError(f"the tool server's answer to {method} has no result object")
        return result

    def _check_initialize(self, result: dict) -> None:
        version, server = result.get("protocolVersion"), result.get("serverInfo")
        if version != PROTOCOL_VERSION:
            raise McpError(f"the tool server speaks MCP {clean_text(str(version), 40)}, not {PROTOCOL_VERSION}")
        if not isinstance(server, dict) or server.get("name") != SERVER_NAME:
            raise McpError(f"the tool server is not {SERVER_NAME}")
        if not isinstance(result.get("capabilities"), dict) or "tools" not in result["capabilities"]:
            raise McpError("the tool server offers no tools capability")
        self.server_info = {
            "name": SERVER_NAME,
            "version": clean_text(str(server.get("version")), 40),
            "protocol": PROTOCOL_VERSION,
        }

    @staticmethod
    def _specs(result: dict) -> list[ToolSpec]:
        if result.get("nextCursor") is not None:
            raise McpError("the tool server lists its tools in pages, which this client does not read")
        listed = result.get("tools")
        if not isinstance(listed, list):
            raise McpError("the tool server's list of tools is not a list")
        specs = []
        for item in listed:
            if not isinstance(item, dict):
                raise McpError("the tool server listed a tool that is not an object")
            name, description, schema = item.get("name"), item.get("description"), item.get("inputSchema")
            annotations, title = item.get("annotations", {}), item.get("title", name)
            if not (isinstance(name, str) and TOOL_NAME.fullmatch(name)):
                raise McpError("the tool server listed a tool whose name is not a plain name")
            if not isinstance(description, str) or not isinstance(title, str):
                raise McpError(f"the tool server's {name} has no description or title")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise McpError(f"the tool server's {name} has no input schema for an object")
            if not isinstance(annotations, dict):
                raise McpError(f"the tool server's {name} has annotations that are not an object")
            specs.append(ToolSpec(name, title, description, schema, annotations))
        return specs

    @staticmethod
    def _tool_result(result: dict) -> ToolResult:
        content, is_error = result.get("content"), result.get("isError", False)
        if not isinstance(content, list) or not isinstance(is_error, bool):
            raise McpError("the tool server's result has no content list, or an isError that is not true or false")
        texts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        if not texts:
            raise McpError("the tool server's result holds no text")
        return ToolResult("\n".join(texts), is_error)


# Recording and replaying a run's tool results.

LABEL = (
    "Tool results as Klaxon's loop received them from its MCP server, each written here before the model saw it."
    " They come from the system under investigation (synthetic data on this Mac), or from fakes in a test."
)


def _append(path: Path, records: list[dict]) -> None:
    """Append records, all or none: a write that fails is cut back off, so no half line is left behind."""
    data = "".join(
        json.dumps(r, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n" for r in records
    ).encode("ascii")
    with open(path, "ab") as out:
        start = out.seek(0, 2)
        try:
            out.write(data)
            out.flush()
        except BaseException:
            out.truncate(start)
            raise


class RecordedTools:
    """A ToolClient that writes every result to a JSON Lines file before passing it on: first the tools as
    listed (a replay rebuilds the same system prompt from them), then each call as it came back."""

    def __init__(self, inner: ToolClient, path: Path | str, header: Mapping | None = None) -> None:
        self.inner = inner
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._header = dict(header or {})
        self._seq = 0

    @property
    def server_info(self) -> dict:
        return getattr(self.inner, "server_info", {})

    def open(self, timeout: float = START_TIMEOUT) -> list[ToolSpec]:
        opened = {"kind": "header", "format": FORMAT, "label": LABEL, "opened_at": utc_now()} | self._header
        try:
            specs = self.inner.open(timeout)
        except McpError as exc:
            _append(self.path, [opened | {"failed": f"{type(exc).__name__}: {exc}"}])
            raise
        listed = {"tools": [asdict(spec) for spec in specs], "server": self.server_info}
        _append(self.path, [opened | listed])
        return specs

    def call(self, name: str, arguments: Mapping[str, object], timeout: float) -> ToolResult:
        self._seq += 1
        record = {"kind": "tool_call", "seq": self._seq, "tool": name, "arguments": dict(arguments)}
        started = time.monotonic()
        try:
            result = self.inner.call(name, arguments, timeout)
        except McpError as exc:
            outcome = "timeout" if isinstance(exc, McpTimeout) else "failed"
            seconds = round(time.monotonic() - started, 3)
            _append(self.path, [record | {"outcome": outcome, "error": str(exc), "seconds": seconds, "at": utc_now()}])
            raise
        seconds = round(time.monotonic() - started, 3)
        record |= {"outcome": "result", "is_error": result.is_error, "text": result.text, "seconds": seconds}
        _append(self.path, [record | {"at": utc_now()}])  # before the loop, and so the model, sees it
        return result

    def close(self) -> None:
        self.inner.close()


def _same_arguments(a: object, b: object) -> bool:
    """Equal as JSON, where true is not 1: compared in a canonical form."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class ReplayTools:
    """A recorded run's tool results, served again: the tools as listed then, and each call's result for the same
    call in the same order. Nothing is called. A different call gets McpError: the replay has gone another way."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        records = read_records(self.path)
        headers = [r for r in records if r.get("kind") == "header" and "tools" in r]
        if len(headers) != 1:
            raise ValueError(f"{self.path.name} should hold one list of tools, and holds {len(headers)}")
        try:
            self._specs = [ToolSpec(**item) for item in headers[0]["tools"]]
        except TypeError:
            raise ValueError(f"the tools listed in {self.path.name} are damaged or edited") from None
        self.server_info = headers[0].get("server", {})
        self._calls = deque(r for r in records if r.get("kind") == "tool_call")
        for record in self._calls:  # a damaged recording is refused here, before anything is served
            fits = isinstance(record.get("tool"), str) and isinstance(record.get("arguments"), dict)
            if record.get("outcome") == "result":
                fits = fits and isinstance(record.get("text"), str) and isinstance(record.get("is_error"), bool)
            elif record.get("outcome") not in ("timeout", "failed"):
                fits = False
            if not fits:
                raise ValueError(f"a tool call in {self.path.name} is damaged or edited")
        self.served = 0

    @property
    def unused(self) -> int:
        return len(self._calls)

    def open(self, timeout: float = START_TIMEOUT) -> list[ToolSpec]:
        return list(self._specs)

    def call(self, name: str, arguments: Mapping[str, object], timeout: float) -> ToolResult:
        if not self._calls:
            raise McpError(f"{self.path.name} has no more tool calls: this run went further than the recorded one")
        record = self._calls[0]
        if record["tool"] != name or not _same_arguments(record["arguments"], dict(arguments)):
            raise McpError(f"{self.path.name} recorded another tool call at this point: the replay went another way")
        self._calls.popleft()
        self.served += 1
        if record["outcome"] == "timeout":
            raise McpTimeout(f"replayed from {self.path.name}: {record.get('error')}")
        if record["outcome"] == "failed":
            raise McpError(f"replayed from {self.path.name}: {record.get('error')}")
        return ToolResult(record["text"], record["is_error"])

    def close(self) -> None:
        pass
