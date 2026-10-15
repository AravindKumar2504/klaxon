"""Klaxon's live model adapter: Claude, through Claude Code in headless mode (claude -p) on Aravind's seat.

Aravind has no API key (DECISIONS.md, 2026-10-14), so each step of Klaxon's loop goes to `claude -p`, which
uses the seat's own login. Claude Code is an agent in its own right, with tools, MCP servers and settings.
Klaxon runs every tool itself, through its MCP server, so here Claude Code is set up only to answer:

- It starts with no tools (--tools ""), no MCP servers (--strict-mcp-config with an empty list), no skills,
  and none of this Mac's customizations: --safe-mode (no CLAUDE.md, skills, installed plugins, hooks or MCP
  servers) and --restricted (no user, project or local settings, so not the memory folder Aravind's user
  settings name either). Nothing may ask for permission, and no transcript is kept. Admin policy, if his
  university sets any, still applies: Klaxon cannot turn that off, and should not.
- Its first event, Claude Code's own account of what it loaded, is checked as it arrives: a tool, an MCP
  server, a memory file, a folder that is not Klaxon's, a sign-in other than the seat's, or another
  permission mode, and the process is stopped and the reply refused. So is a reply that ran a tool or took
  more than one turn.
- It runs in a new, empty temporary folder, removed afterwards, with a small environment: no API key, so
  calls go through the seat's login; no API address or proxy, which could send the prompt and the login
  elsewhere; no variables of a Claude Code session that started Klaxon; CLAUDE.md and auto memory off.
- The conversation goes in on stdin, never on the command line. Each turn is wrapped in a tag made from the
  conversation's own hash, which no text inside a turn can know, so no log line can end its turn or start
  one. Every at sign is written as a fullwidth one, so Claude Code never takes "@path" in a log line for a
  file to attach. Hidden characters are dropped.
- A deadline bounds the whole call. At the timeout the whole process group is killed, and Klaxon never waits
  for anything else that still holds Claude Code's output open. The output is untrusted: capped, parsed as
  JSON line by line, checked field by field, never run.

Every reply is recorded (klaxon.agent.model.Recording) before the loop sees it, and so is every failure. A
reply that cannot be recorded is not returned.

Run: python -m klaxon.agent.claude_code command   print the exact command of a live call; calls nothing
     python -m klaxon.agent.claude_code check     one tiny live call ("Reply with the word ok"), saved as evidence
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from klaxon.agent.model import (
    Message,
    ModelError,
    ModelMismatch,
    ModelTimeout,
    ModelUnsafe,
    Recording,
    Reply,
    Usage,
    check_conversation,
    finite,
    prompt_sha256,
    text_sha256,
    utc_now,
)
from klaxon.agent.tools import HOME, clean, clean_text

DEFAULT_MODEL = "claude-opus-5-5"  # Aravind's choice for week 3 (DECISIONS.md, 2026-10-14)
DEFAULT_TIMEOUT = 300.0  # seconds for one reply
EXIT_GRACE = 10.0  # seconds Claude Code gets to exit on its own after its reply, before it is stopped
# Where the Claude desktop app keeps its own copies of Claude Code: <version>/<build>/claude.app/...
APP_COPIES = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
APP_BINARY = "claude.app/Contents/MacOS/claude"
MAX_OUTPUT = 5_000_000  # bytes Claude Code may write for one reply, stdout and stderr together
MAX_STDERR = 20_000  # bytes of stderr kept: its last words
MAX_STDIN = 9_000_000  # bytes: its documentation caps piped stdin at 10 MB
MAX_SYSTEM = 100_000  # characters: the system prompt goes on the command line, which has a size limit
MAX_DEPTH = 64  # nesting of one event; anything deeper is not an event Claude Code sends
REQUESTED = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,90}")  # a full model id, never an alias such as "opus"
REPORTED = re.compile(r"[a-z0-9][a-z0-9.-]{0,99}")
SEAT = "none"  # the init event's apiKeySource when Claude Code uses a claude.ai login, not an API key
PERMISSION_MODE = "dontAsk"

OUTPUT = ("--print", "--output-format", "stream-json", "--verbose")  # one reply, as JSON events, one per line
# What Claude Code may use for a reply: the model, and nothing else.
# fmt: off
LOCKDOWN = (
    "--tools", "",                                   # no built-in tool: nothing to run, read, write or fetch
    "--strict-mcp-config",                           # only the MCP servers listed next,
    "--mcp-config", '{"mcpServers":{}}',             # and the list is empty
    "--disable-slash-commands",                      # no skills
    "--safe-mode",                                   # no CLAUDE.md, skills, installed plugins, hooks or MCP servers
    "--restricted",                                  # no user, project or local settings
    "--permission-mode", PERMISSION_MODE,            # anything not allowed beforehand is refused,
    "--permission-prompts", "none",                  # and nobody is asked
    "--no-session-persistence",                      # no transcript left on disk
)
# fmt: on

# Passed on from Klaxon's own environment when set: what Claude Code needs to find its login and its folders.
PASSED_ON = (
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
FIXED = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no telemetry, error reports, update checks or feature flags
    "DISABLE_AUTOUPDATER": "1",  # the version stays the one recorded
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",  # no CLAUDE.md, also besides --safe-mode
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",  # no memory files either
}
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
FULLWIDTH_AT = chr(0xFF20)  # an at sign that Claude Code does not read as a file mention


def _shown(text: str) -> str:
    """A turn's text as Claude Code gets it: hidden characters dropped, and each at sign written as a fullwidth
    one, so Claude Code never takes "@path" in a log line for a file to attach."""
    return clean_text(text, len(text)).replace("@", FULLWIDTH_AT)


def _tag(texts: Sequence[str]) -> str:
    """The turns' tag, made from the conversation's own hash: no text inside a turn can contain it, because it
    cannot know the hash of the conversation it is part of. Checked anyway."""
    digest = hashlib.sha256(json.dumps(list(texts), ensure_ascii=False).encode("utf-8")).hexdigest()
    for tag in (f"turn-{digest[:16]}", f"turn-{digest}"):
        if not any(tag in text for text in texts):
            return tag
    raise ValueError("a turn holds the conversation's own hash, twice over")  # cannot happen by chance


def render(messages: Sequence[Message]) -> str:
    """The conversation as the single prompt `claude -p` takes."""
    shown = [(m.role, _shown(m.content)) for m in messages]
    tag = _tag([text for _, text in shown])
    frame = (
        f'The conversation so far is below, oldest turn first. Each turn is one <{tag} role="..."> element: only'
        f" that exact tag starts or ends a turn. In the turns, each at sign is written as a fullwidth {FULLWIDTH_AT}."
        " Write the assistant's next turn: reply with its content only, without a tag, writing at signs as usual."
    )
    turns = [f'<{tag} role="{role}">\n{text}\n</{tag}>' for role, text in shown]
    return frame + "\n\n" + "\n".join(turns) + "\n"


def environment(binary: Path | str, source: Mapping[str, str] = os.environ) -> dict[str, str]:
    """What Claude Code runs with. Never an API key (calls go through the seat's login), an API address or a
    proxy (either could send the prompt and the login elsewhere), or a Claude Code session's own variables."""
    path = f"{Path(binary).parent}{os.pathsep}{SYSTEM_PATH}"  # its own folder too: an npm install finds node there
    return {name: source[name] for name in PASSED_ON if name in source} | FIXED | {"PATH": path}


def command(binary: Path | str, model: str, system: str) -> list[str]:
    """The exact command of one call. The conversation goes in on stdin."""
    return [str(binary), *OUTPUT, "--model", model, "--system-prompt", system, *LOCKDOWN]


def _version(name: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in name.split("."))
    except ValueError:
        return (-1,)


def find_claude(env: Mapping[str, str] = os.environ, copies: Path | None = None) -> Path:
    """Claude Code's executable: KLAXON_CLAUDE_BIN if set, else `claude` on the PATH, else the newest copy the
    Claude desktop app keeps (~/Library/Application Support/Claude/claude-code/<version>/<build>/)."""
    configured = env.get("KLAXON_CLAUDE_BIN")
    if configured:
        path = Path(configured).expanduser()
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise ModelError(f"KLAXON_CLAUDE_BIN is not an executable file: {clean_text(str(path), 300)}")
        return path
    on_path = shutil.which("claude", path=env.get("PATH"))
    if on_path:
        return Path(on_path)
    copies = copies or APP_COPIES
    found = [p for p in copies.glob(f"*/*/{APP_BINARY}") if p.is_file() and os.access(p, os.X_OK)]
    if found:
        return max(found, key=lambda p: _version(p.relative_to(copies).parts[0]))
    raise ModelError(
        "Claude Code was not found: set KLAXON_CLAUDE_BIN to its path. The Claude desktop app keeps a copy in"
        " ~/Library/Application Support/Claude/claude-code/<version>/<build>/claude.app/Contents/MacOS/claude"
    )


def _check_timeout(seconds: object) -> float:
    value = finite(seconds)
    if value is None or not 0 < value <= 3600:
        raise ValueError("the timeout must be more than 0 and at most 3600 seconds")
    return value


def _check_system(system: object) -> None:
    """A system prompt Klaxon can put on the command line, refused before anything runs."""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("a system prompt is needed: without one, Claude Code would use its own")
    if system.startswith("-"):
        raise ValueError("a system prompt must not start with '-': it goes on the command line")
    if len(system) > MAX_SYSTEM:
        raise ValueError(f"a system prompt must not pass {MAX_SYSTEM:,} characters: it goes on the command line")
    if "\x00" in system or any("\ud800" <= ch <= "\udfff" for ch in system):
        raise ValueError("a system prompt cannot hold a NUL or a lone surrogate: a command line cannot carry them")


@dataclass
class _Run:
    """What one call did, before it is judged."""

    events: list[dict] = field(default_factory=list)
    seen_init: bool = False
    finished: bool = False  # its result event arrived, before the deadline
    result_at: str | None = None  # when it arrived
    unreadable_lines: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    lingered: bool = False  # it did not exit within EXIT_GRACE of its reply, and was stopped
    stopped: tuple[type[ModelError], str] | None = None  # why Klaxon stopped it early
    stderr: bytes = b""
    left_in_folder: list[str] = field(default_factory=list)  # should stay empty; recorded, not enforced


def _kill_group(proc: subprocess.Popen) -> None:
    """Claude Code and everything in its process group: it leads its own (start_new_session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # already gone


def _feed(pipe, data: bytes) -> None:
    """Write the prompt from a thread of its own, so a full pipe cannot block the reading of Claude Code's output."""
    try:
        pipe.write(data)
    except (OSError, ValueError):
        pass  # Claude Code stopped before reading it all (its output or exit code says why), or the call ended
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not a JSON number")  # json.loads would otherwise read NaN and Infinity


def _too_deep(value: object) -> bool:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            if depth > MAX_DEPTH:
                return True
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return False


def _event(line: bytes) -> dict | None:
    """One line of output as an event, or None: not JSON, not UTF-8, not an event, or nested too deep."""
    try:
        event = json.loads(line, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str) or _too_deep(event):
        return None
    return event


def _is_init(event: dict) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "init"


def _any_text(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_any_text(item) for item in value.values())
    if isinstance(value, list):
        return any(_any_text(item) for item in value)
    return False


def _loaded(init: dict, folder: str) -> str | None:
    """Why the call must stop, from Claude Code's own account of what it loaded; None if it loaded nothing."""
    tools, servers, cwd = init.get("tools"), init.get("mcp_servers"), init.get("cwd")
    if not isinstance(tools, list) or not isinstance(servers, list) or not isinstance(cwd, str):
        return "Claude Code did not say which tools, MCP servers and folder it uses"
    if tools:
        return f"Claude Code loaded {len(tools)} tool(s)"
    if servers:
        return f"Claude Code loaded {len(servers)} MCP server(s)"
    if _any_text(init.get("memory_paths")):
        return "Claude Code loaded memory files"
    if os.path.realpath(cwd) != os.path.realpath(folder):
        return "Claude Code is not working in the empty folder Klaxon made for it"
    if init.get("apiKeySource") != SEAT:
        return "Claude Code is not signed in with the seat's own login (an API key or a key helper instead)"
    if init.get("permissionMode") != PERMISSION_MODE:
        return f"Claude Code is not in the permission mode Klaxon asked for ({PERMISSION_MODE})"
    return None


def _ran_something(event: dict) -> str | None:
    """A tool call or a tool result in the stream: something ran, or was about to."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind in ("tool_use", "server_tool_use", "mcp_tool_use"):
            return "the model asked for a tool"
        if kind in ("tool_result", "mcp_tool_result"):
            return "a tool ran"
    return None


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # a terminal color or cursor code


def _said(value: object, last: bool = False) -> str:
    """One line of what Claude Code said, for an error message: the first line of a result, the last of stderr
    (its last words), without terminal colors, cleaned and capped."""
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, str) and item.strip()), "")
    plain = ANSI.sub("", str(value or ""))
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    return f": {clean_text(lines[-1 if last else 0], 300)}" if lines else ""


HOME_SLUG = HOME.replace("/", "-")  # how Claude Code names a folder after a path: -Users-name-...


def _home_as_tilde(value: object) -> object:
    """A copy with this Mac's home folder written as ~, as in tool output, also in the dashed form Claude Code uses
    for its own folder names; nothing else changes."""
    if isinstance(value, str):
        return value.replace(HOME, "~").replace(HOME_SLUG, "~") if len(HOME) > 1 else value
    if isinstance(value, list):
        return [_home_as_tilde(item) for item in value]
    if isinstance(value, dict):
        return {key: _home_as_tilde(item) for key, item in value.items()}
    return value


def _same_model(reported: str, requested: str) -> bool:
    """The model asked for, or a dated snapshot of it (claude-opus-5-5-20261001), never another model."""
    return re.fullmatch(re.escape(requested) + r"(-\d{8})?", reported) is not None


class ClaudeCode:
    """Live: Claude through `claude -p` on Aravind's seat. Every reply and every failure goes to the recording."""

    source = "claude-code"

    def __init__(
        self,
        recording: Recording,
        model: str = DEFAULT_MODEL,
        binary: Path | str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        require_model: bool = True,
    ) -> None:
        if not isinstance(recording, Recording):
            raise TypeError("a live adapter needs a Recording: every live reply is saved before it is used")
        if not REQUESTED.fullmatch(model):
            raise ValueError(f"model must be a full model id such as {DEFAULT_MODEL}, not an alias: {model!r}")
        self.timeout_seconds = _check_timeout(timeout_seconds)
        self.recording = recording
        self.model = model
        self.binary = Path(binary) if binary else find_claude()
        self.require_model = require_model

    def ask(self, system: str, messages: Sequence[Message]) -> Reply:
        turns = check_conversation(messages)
        _check_system(system)
        stdin = render(turns).encode("utf-8")
        if len(stdin) > MAX_STDIN:
            raise ValueError(f"the conversation is over {MAX_STDIN:,} bytes: Claude Code reads at most 10 MB")
        started = time.monotonic()
        run = _Run()
        try:
            run = self._run(system, stdin)
            reply = self._reply(run, prompt_sha256(system, turns), round(time.monotonic() - started, 3))
        except Exception as exc:  # the model's failures, and Klaxon's own (a full disk, say): all recorded
            error = (
                exc if isinstance(exc, ModelError) else ModelError(f"the call failed in Klaxon ({type(exc).__name__})")
            )
            seconds = round(time.monotonic() - started, 3)
            details = self._details(system, stdin, run)
            self._record(self.recording.failure, system, turns, error, seconds, details, _home_as_tilde(run.events))
            if error is exc:
                raise
            raise error from exc
        details = self._details(system, stdin, run)
        if self.require_model and not _same_model(reply.model, self.model):
            refused = ModelMismatch(f"asked for {self.model}, but the reply says {reply.model} wrote it")
            self._record(self.recording.response, system, turns, reply, details, refused)
            raise refused
        self._record(self.recording.response, system, turns, reply, details)  # before the loop ever sees it
        return reply

    @staticmethod
    def _record(write: Callable[..., object], *args: object) -> None:
        try:
            write(*args)
        except Exception as exc:  # a disk that is full, a folder that is gone, a value JSON cannot hold
            raise ModelError(
                f"the call could not be recorded, so its reply is not used ({type(exc).__name__})"
            ) from exc

    def _run(self, system: str, stdin: bytes) -> _Run:
        run = _Run()
        with tempfile.TemporaryDirectory(prefix="klaxon-claude-") as folder:
            try:
                proc = subprocess.Popen(
                    command(self.binary, self.model, system),
                    cwd=folder,
                    env=environment(self.binary),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,  # its own process group, so a timeout can stop all of it
                )
            except (OSError, ValueError) as exc:
                run.stopped = (ModelError, f"Claude Code could not be started ({type(exc).__name__})")
                return run
            feeder = threading.Thread(target=_feed, args=(proc.stdin, stdin), daemon=True)
            try:
                feeder.start()
                self._read(proc, folder, run)
                if run.stopped or run.timed_out:
                    _kill_group(proc)
                else:  # its reply is in, or its output ended: time to exit on its own
                    try:
                        proc.wait(timeout=EXIT_GRACE)
                    except subprocess.TimeoutExpired:
                        run.lingered = run.finished
                        _kill_group(proc)
                try:
                    proc.wait(timeout=EXIT_GRACE)
                except subprocess.TimeoutExpired:
                    pass  # killed, but not reaped yet; its exit code stays unknown
            finally:
                _kill_group(proc)  # whatever in its group is still there, on every path out
                for pipe in (proc.stdout, proc.stderr):
                    pipe.close()
                if feeder.is_alive():
                    feeder.join(timeout=5)
                if not proc.stdin.closed:
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
            run.exit_code = proc.returncode
            run.left_in_folder = sorted(os.listdir(folder))[:20]
        return run

    def _read(self, proc: subprocess.Popen, folder: str, run: _Run) -> None:
        """Claude Code's events, line by line, until its result, a reason to stop it, the deadline, or the end of
        its output. Its stderr is read too, so neither pipe can fill up; only its last words are kept."""
        deadline = time.monotonic() + self.timeout_seconds
        out, err = proc.stdout.fileno(), proc.stderr.fileno()
        pending, size = b"", 0
        with selectors.DefaultSelector() as selector:
            for fd in (out, err):
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    run.timed_out = True
                    return
                ready = selector.select(min(remaining, 0.25))
                if not ready and proc.poll() is not None:
                    break  # it exited and nothing more is coming: what still holds its pipes is not waited for
                for key, _ in ready:
                    try:
                        chunk = os.read(key.fd, 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    size += len(chunk)
                    if size > MAX_OUTPUT:
                        run.stopped = (ModelError, f"Claude Code wrote more than {MAX_OUTPUT:,} bytes")
                        return
                    if key.fd == err:
                        run.stderr = (run.stderr + chunk)[-MAX_STDERR:]
                        continue
                    *lines, pending = (pending + chunk).split(b"\n")
                    for line in lines:
                        if self._take(line, folder, run):
                            return
        if pending.strip():
            self._take(pending, folder, run)

    @staticmethod
    def _take(line: bytes, folder: str, run: _Run) -> bool:
        """One line of output. True when reading should stop: the reply is complete, or Klaxon refuses it."""
        if not line.strip():
            return False
        event = _event(line)
        if event is None:
            run.unreadable_lines += 1
            return False
        run.events.append(event)
        if _is_init(event):
            run.seen_init = True
            reason = _loaded(event, folder)
        elif event.get("type") in ("assistant", "user", "result") and not run.seen_init:
            reason = "Claude Code answered before saying what it loaded"
        else:
            reason = _ran_something(event)
        if reason:
            run.stopped = (ModelUnsafe, reason)
            return True
        if event.get("type") == "result":  # its last event: the reply is complete, and in time
            run.finished, run.result_at = True, utc_now()
            return True
        return False

    def _reply(self, run: _Run, digest: str, seconds: float) -> Reply:
        if run.stopped:
            kind, reason = run.stopped
            raise kind(reason)
        if run.timed_out:  # whatever it printed before: a call that passes its deadline fails
            raise ModelTimeout(f"no reply within {self.timeout_seconds:g} s; Claude Code was stopped")
        stderr = run.stderr.decode("utf-8", "replace")
        result = next((e for e in reversed(run.events) if e.get("type") == "result"), None)
        if result is None:
            raise ModelError(
                f"Claude Code stopped without a result (exit code {run.exit_code}){_said(stderr, last=True)}"
            )
        if result.get("is_error") is not False or result.get("subtype") != "success":
            subtype = clean_text(str(result.get("subtype")), 60)
            raise ModelError(
                f"Claude Code reported an error ({subtype}){_said(result.get('result') or result.get('errors'))}"
            )
        if run.exit_code != 0 and not run.lingered:  # a reply complete before Klaxon stopped it still counts
            raise ModelError(f"Claude Code exited with code {run.exit_code}{_said(stderr, last=True)}")
        if result.get("num_turns") != 1:
            raise ModelUnsafe(f"Claude Code took {clean_text(str(result.get('num_turns')), 20)} turns for one reply")
        if result.get("permission_denials"):
            raise ModelUnsafe("Claude Code was refused a permission: something tried to run")
        text = result.get("result")
        if not isinstance(text, str):
            raise ModelError("Claude Code's result has no text")
        models = {
            e["message"].get("model")
            for e in run.events
            if e.get("type") == "assistant" and isinstance(e.get("message"), dict)
        }
        if len(models) != 1:
            raise ModelError("the reply does not name the one model that wrote it")
        model = models.pop()
        if not (isinstance(model, str) and REPORTED.fullmatch(model)):
            raise ModelError("the reply names its model in a form Klaxon does not accept")
        return Reply(
            text=text,
            model=model,
            usage=Usage.from_api(result.get("usage")),
            seconds=seconds,
            source=self.source,
            prompt_sha256=digest,
            received_at=run.result_at or utc_now(),
            cost_usd=finite(result.get("total_cost_usd")),
            raw=_home_as_tilde(run.events),
        )

    def _details(self, system: str, stdin: bytes, run: _Run) -> dict:
        """How the call was made, for the recording: everything but the prompt, which is stored by part."""
        init = next((e for e in run.events if _is_init(e)), {})
        shown = command(self.binary, self.model, f"<the system prompt, sha256 {text_sha256(system)}>")
        details = {
            "requested_model": self.model,
            "claude_code": clean(
                {
                    "binary": str(self.binary),
                    "version": init.get("claude_code_version"),
                    "model": init.get("model"),
                    "auth": init.get("apiKeySource"),
                    "permission_mode": init.get("permissionMode"),
                    "tools": init.get("tools"),
                    "mcp_servers": init.get("mcp_servers"),
                    "memory_paths": init.get("memory_paths"),
                }
            ),
            "command": [clean_text(arg, 300) for arg in shown],
            "environment": sorted(environment(self.binary)),
            "working_folder": "a new empty temporary folder, removed after the call",
            "left_in_folder": [clean_text(name, 100) for name in run.left_in_folder],
            "stdin_sha256": hashlib.sha256(stdin).hexdigest(),
            "stdin_bytes": len(stdin),
            "timeout_seconds": self.timeout_seconds,
            "exit_code": run.exit_code,
            "timed_out": run.timed_out,
            "stopped_after_its_reply": run.lingered,
            "unreadable_lines": run.unreadable_lines,
            "stderr": clean_text(run.stderr.decode("utf-8", "replace")[-2000:], 2000),
        }
        return _home_as_tilde(details)


# The live check: one tiny call, to show that headless mode works on the seat.

CHECK_SYSTEM = "You are answering a connectivity check for a software project. Follow the user's instruction exactly."
CHECK_PROMPT = "Reply with the word ok"
# The check's prompt is a few sentences. Much more input would mean something else reached the model: CLAUDE.md, memory.
CHECK_MAX_INPUT_TOKENS = 1_500


def _provenance() -> dict:
    from klaxon import __version__
    from klaxon.demo.duplicate_webhook import code_fingerprint
    from klaxon.patient.stack import REPO

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    status = git("status", "--porcelain", "--", "src", "Makefile", "pyproject.toml")
    now = datetime.now(timezone.utc)
    return {
        "ran_at_utc": now.isoformat(timespec="seconds"),
        "ran_at_local": now.astimezone().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "klaxon_version": __version__,
        "code_sha256": code_fingerprint(),
        "git_commit": git("rev-parse", "HEAD"),
        "git_clean_code": None if status is None else status == "",
    }


def _new_path(directory: Path, stem: str) -> Path:
    path, n = directory / f"{stem}.jsonl", 2
    while path.exists():
        path, n = directory / f"{stem}-{n}.jsonl", n + 1
    return path


def main(argv: list[str] | None = None) -> int:
    from klaxon.patient.stack import REPO

    parser = argparse.ArgumentParser(prog="python -m klaxon.agent.claude_code", description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action", required=True)
    show = actions.add_parser("command", help="print the exact command of a live call, and call nothing")
    check = actions.add_parser("check", help="one tiny live call on the seat, recorded as evidence")
    for sub in (show, check):
        sub.add_argument("--model", default=DEFAULT_MODEL)
    check.add_argument("--evidence-dir", type=Path, default=REPO / "evidence" / "week3")
    check.add_argument("--timeout", type=float, default=120.0, help="seconds (default 120)")
    args = parser.parse_args(argv)
    try:
        binary = find_claude()
        if not REQUESTED.fullmatch(args.model):
            raise ValueError(f"--model must be a full model id such as {DEFAULT_MODEL}")
    except (ModelError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.action == "command":
        shown = command("claude", args.model, "<the system prompt>")
        print(f"claude: {clean_text(str(binary))}")
        print("The command of each live call (the conversation goes in on stdin):")
        print(f"  {clean_text(shlex.join(shown), 2000)}")
        print("Working folder: a new empty temporary folder, removed after the call")
        fixed = ", ".join(f"{name}={value}" for name, value in FIXED.items())
        print(
            f"Environment: {', '.join(sorted(PASSED_ON))} when set, {fixed}, and PATH: claude's folder, {SYSTEM_PATH}"
        )
        print(f"Timeout: {DEFAULT_TIMEOUT:.0f} s, then the whole process group is killed")
        return 0
    try:
        _check_timeout(args.timeout)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    path = _new_path(args.evidence_dir, f"{stamp}_claude-check")
    header = {
        "what": "One tiny live call through Klaxon's live model adapter, to check that headless mode works on Aravind's seat.",
        "rerun_command": "make claude-check",
        **_provenance(),
    }
    adapter = ClaudeCode(Recording(path, header), model=args.model, binary=binary, timeout_seconds=args.timeout)
    try:
        reply = adapter.ask(CHECK_SYSTEM, [Message("user", CHECK_PROMPT)])
    except ModelError as exc:
        print(f"The live call failed ({type(exc).__name__}): {exc}\nRecorded in {path}", file=sys.stderr)
        return 2
    usage = reply.usage
    print(f"Asked {args.model} through {clean_text(str(binary))}")
    print(
        f"Reply: {clean_text(reply.text, 200)!r}, from {reply.model}, received {reply.received_at}, in {reply.seconds:.1f} s"
    )
    print(
        f"Tokens: {usage.input_tokens} in ({usage.cache_read_tokens} read from the cache, {usage.cache_write_tokens}"
        f" written to it), {usage.output_tokens} out"
    )
    if reply.cost_usd is not None:
        print(f"Claude Code's estimate at API prices: ${reply.cost_usd:.4f} (the seat is not billed per call)")
    print(f"Recorded in {path}")
    if reply.text.strip().strip(".!").lower() != "ok":
        print("The reply was not 'ok'.", file=sys.stderr)
        return 1
    if usage.total_input_tokens is None or usage.total_input_tokens > CHECK_MAX_INPUT_TOKENS:
        print(
            f"The model read {usage.total_input_tokens} input tokens, more than the {CHECK_MAX_INPUT_TOKENS:,} Klaxon's"
            " prompt can account for: something else reached it.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
