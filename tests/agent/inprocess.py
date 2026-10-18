"""Klaxon's real MCP server code in the test's own process: its request handling and its stdio framing
(mcp_server.serve), behind the MCP client's transport interface. No subprocess, so tests stay fast."""

import io
import json
from collections import deque

from klaxon.agent import mcp_server


class InProcess:
    def __init__(self, server: mcp_server.Server) -> None:
        self.server = server
        self.waiting: deque[str] = deque()
        self.sent: list[dict] = []  # every message the client sent, as the server got it
        self.closed = False

    def send(self, line: str) -> None:
        self.sent.append(json.loads(line))
        out = io.StringIO()
        mcp_server.serve(self.server, io.StringIO(line + "\n"), out)
        self.waiting.extend(out.getvalue().splitlines())

    def receive(self, deadline: float) -> str | None:
        return self.waiting.popleft() if self.waiting else None

    def close(self, graceful: bool = True) -> None:
        self.closed = True

    def last_words(self) -> str:
        return ""

    def tool_calls(self) -> list[dict]:
        """The tools/call requests the server got: each one's name and arguments."""
        return [message["params"] for message in self.sent if message.get("method") == "tools/call"]
