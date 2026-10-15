"""Klaxon's live model adapter: Claude, through Claude Code in headless mode (claude -p) on Aravind's seat.

Aravind has no API key (DECISIONS.md, 2026-10-14), so each step of Klaxon's loop goes to `claude -p`, which
uses the seat's own login. Claude Code is an agent in its own right, with tools, MCP servers and settings.
Klaxon runs every tool itself, through its MCP server, so here Claude Code must only answer:

- No tools, no MCP servers, no skills: --tools "", --strict-mcp-config with an empty server list, and
  --disable-slash-commands. None of this Mac's customizations either: --safe-mode (no CLAUDE.md, auto
  memory, hooks, plugins or MCP servers) and --restricted (no user, project or local settings). Nothing may
  ask for permission, and no transcript is kept.
- Claude Code's own account of what it loaded (its first "init" event) is checked as it arrives. One tool,
  one MCP server, or a working folder that is not the one Klaxon made, and the process is stopped and the
  reply refused. So is a reply that ran a tool or took more than one turn.
- It runs in a new, empty temporary folder, removed afterwards, with a small environment: no API key, so
  calls go through the seat's login; no API address or proxy, which could send the prompt and the login
  elsewhere; no variables of a Claude Code session that started Klaxon.
- The conversation goes in on stdin, never on the command line. At the timeout, the whole process group is
  killed: nothing Claude Code started outlives the call.
- Its output is untrusted: read line by line, capped, parsed as JSON, checked field by field, never run.

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
# Where the Claude desktop app keeps its own copies of Claude Code: <version>/<build>/claude.app/...
APP_COPIES = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
APP_BINARY = "claude.app/Contents/MacOS/claude"
MAX_OUTPUT = 5_000_000  # bytes of Claude Code's output read for one reply
EXIT_GRACE = 10.0  # seconds Claude Code gets to exit on its own after its reply, before it is stopped
MAX_STDIN = 9_000_000  # bytes: Claude Code reads at most 10 MB from stdin
MAX_SYSTEM = 100_000  # characters: the system prompt goes on the command line, which has a size limit
REQUESTED = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,90}")  # a full model id, never an alias such as "opus"
REPORTED = re.compile(r"[a-z0-9][a-z0-9.-]{0,99}")

OUTPUT = ("--print", "--output-format", "stream-json", "--verbose")  # one reply, as JSON events, one per line
# What Claude Code may use for a reply: the model, and nothing else.
# fmt: off
LOCKDOWN = (
    "--tools", "",                                   # no built-in tool: nothing to run, read, write or fetch
    "--strict-mcp-config",                           # only the MCP servers listed next,
    "--mcp-config", '{"mcpServers":{}}',             # and the list is empty
    "--disable-slash-commands",                      # no skills
    "--safe-mode",                                   # no CLAUDE.md, auto memory, hooks, plugins or MCP servers
    "--restricted",                                  # no user, project or local settings
    "--permission-mode", "dontAsk",                  # anything not allowed beforehand is refused,
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
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no telemetry, error reports, update checks or feature flags
    "DISABLE_AUTOUPDATER": "1",  # the version stays the one recorded
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",  # no CLAUDE.md, also besides --safe-mode
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",  # no memory files either
}

FRAME = (
    "The conversation so far is below, oldest turn first, each turn inside a <turn> element. Write the"
    " assistant's next turn: reply with its content only, without a <turn> tag."
)
TURN_TAG = re.compile(r"<(\s*/?\s*)(turn)", re.IGNORECASE)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # a terminal color or cursor code


def _defuse(text: str) -> str:
    """Break anything that looks like a turn tag ("</turn" becomes "</_turn"), so no turn can end early."""
    return TURN_TAG.sub(r"<\1_\2", text)


def render(messages: Sequence[Message]) -> str:
    """The conversation as the single prompt `claude -p` takes. No text inside a turn, a log line included, can
    end its turn or start one of its own."""
    turns = [f'<turn role="{m.role}">\n{_defuse(m.content)}\n</turn>' for m in messages]
    return FRAME + "\n\n" + "\n".join(turns) + "\n"


def environment(source: Mapping[str, str] = os.environ) -> dict[str, str]:
    """What Claude Code runs with. Never an API key (calls go through the seat's login), an API address or a
    proxy (either could send the prompt and the login elsewhere), or a Claude Code session's own variables."""
    return {name: source[name] for name in PASSED_ON if name in source} | FIXED


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


@dataclass
class _Run:
    """What one call did, before it is judged."""

    events: list[dict] = field(default_factory=list)
    unreadable_lines: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    stopped: tuple[type[ModelError], str] | None = None  # why Klaxon stopped it early
    stderr: str = ""
    left_in_folder: list[str] = field(default_factory=list)  # the folder must stay empty
    lingered: bool = False  # it did not exit within EXIT_GRACE of its reply, and was stopped


def _kill_group(proc: subprocess.Popen) -> None:
    """Claude Code and everything it started: it leads its own process group (start_new_session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # already gone


def _feed(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
    except OSError:
        pass  # Claude Code stopped before reading it all; its output or exit code says why
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _event(line: bytes) -> dict | None:
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):  # not JSON, or not UTF-8
        return None
    return event if isinstance(event, dict) and isinstance(event.get("type"), str) else None


def _is_init(event: dict) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "init"


def _loaded(init: dict, folder: str) -> str | None:
    """Why the call must stop, from Claude Code's own account of what it loaded; None if it loaded nothing."""
    tools, servers, cwd = init.get("tools"), init.get("mcp_servers"), init.get("cwd")
    if not isinstance(tools, list) or not isinstance(servers, list) or not isinstance(cwd, str):
        return "Claude Code did not say which tools, MCP servers and folder it uses"
    if tools:
        return f"Claude Code loaded {len(tools)} tool(s)"
    if servers:
        return f"Claude Code loaded {len(servers)} MCP server(s)"
    if os.path.realpath(cwd) != os.path.realpath(folder):
        return "Claude Code is not working in the empty folder Klaxon made for it"
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


def _said(value: object, last: bool = False) -> str:
    """One line of what Claude Code said, for an error message: the first line of a result, the last of stderr
    (its last words), without terminal colors, cleaned and capped."""
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, str) and item.strip()), "")
    plain = ANSI.sub("", str(value or ""))
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    return f": {clean_text(lines[-1 if last else 0], 300)}" if lines else ""


def _home_as_tilde(value: object) -> object:
    """A copy with this Mac's home folder written as ~, as in tool output; nothing else changes."""
    if isinstance(value, str):
        return value.replace(HOME, "~") if len(HOME) > 1 else value
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
        if not (isinstance(timeout_seconds, int | float) and 0 < timeout_seconds <= 3600):
            raise ValueError("timeout_seconds must be more than 0 and at most 3600")
        self.recording = recording
        self.model = model
        self.binary = Path(binary) if binary else find_claude()
        self.timeout_seconds = float(timeout_seconds)
        self.require_model = require_model

    def ask(self, system: str, messages: Sequence[Message]) -> Reply:
        turns = check_conversation(messages)
        if not isinstance(system, str) or not system.strip():
            raise ValueError("a system prompt is needed: without one, Claude Code would use its own")
        if system.startswith("-") or len(system) > MAX_SYSTEM:
            raise ValueError(
                f"a system prompt must not start with '-' (it goes on the command line) or pass {MAX_SYSTEM:,} characters"
            )
        stdin = render(turns).encode("utf-8", "surrogatepass")
        if len(stdin) > MAX_STDIN:
            raise ValueError(f"the conversation is over {MAX_STDIN:,} bytes: Claude Code reads at most 10 MB")
        started = time.monotonic()
        run = self._run(system, stdin)
        seconds = round(time.monotonic() - started, 3)
        details = self._details(system, stdin, run)
        try:
            reply = self._reply(run, prompt_sha256(system, turns), seconds)
        except ModelError as exc:
            self._record(self.recording.failure, system, turns, exc, seconds, details, _home_as_tilde(run.events))
            raise
        if self.require_model and not _same_model(reply.model, self.model):
            refused = f"asked for {self.model}, but the reply says {reply.model} wrote it"
            self._record(self.recording.response, system, turns, reply, details, refused)
            raise ModelMismatch(refused)
        self._record(self.recording.response, system, turns, reply, details)  # before the loop ever sees it
        return reply

    @staticmethod
    def _record(write: Callable[..., object], *args: object) -> None:
        try:
            write(*args)
        except OSError as exc:
            raise ModelError(
                f"the call could not be recorded, so its reply is not used ({type(exc).__name__})"
            ) from exc

    def _run(self, system: str, stdin: bytes) -> _Run:
        run = _Run()
        with tempfile.TemporaryDirectory(prefix="klaxon-claude-") as folder, tempfile.TemporaryFile() as errors:
            try:
                proc = subprocess.Popen(
                    command(self.binary, self.model, system),
                    cwd=folder,
                    env=environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    start_new_session=True,  # its own process group, so a timeout can stop all of it
                )
            except OSError as exc:
                run.stopped = (ModelError, f"Claude Code could not be started ({type(exc).__name__})")
                return run
            fired = threading.Event()

            def on_timeout() -> None:
                fired.set()
                _kill_group(proc)

            timer = threading.Timer(self.timeout_seconds, on_timeout)
            timer.daemon = True
            timer.start()
            feeder = threading.Thread(target=_feed, args=(proc.stdin, stdin), daemon=True)
            feeder.start()
            size, seen_init, finished = 0, False, False
            try:
                while line := proc.stdout.readline(MAX_OUTPUT + 1 - size):  # one long line cannot pass the cap
                    size += len(line)
                    if size > MAX_OUTPUT:
                        run.stopped = (ModelError, f"Claude Code wrote more than {MAX_OUTPUT:,} bytes")
                        break
                    event = _event(line)
                    if event is None:
                        run.unreadable_lines += 1
                        continue
                    run.events.append(event)
                    if _is_init(event):
                        seen_init = True
                        reason = _loaded(event, folder)
                    elif event.get("type") in ("assistant", "user", "result") and not seen_init:
                        reason = "Claude Code answered before saying what it loaded"
                    else:
                        reason = _ran_something(event)
                    if reason:
                        run.stopped = (ModelUnsafe, reason)
                        break
                    if event.get("type") == "result":  # its last event: the reply is complete, and in time
                        timer.cancel()
                        finished = True
                        break  # a process it started may still hold the pipe open; the reply does not wait for it
                if run.stopped:
                    _kill_group(proc)
                elif finished:
                    try:
                        proc.wait(timeout=EXIT_GRACE)  # time to exit on its own
                    except subprocess.TimeoutExpired:
                        run.lingered = True
                        _kill_group(proc)
                proc.wait()
            finally:
                timer.cancel()
                _kill_group(proc)  # anything it started and left behind
                proc.stdout.close()
                feeder.join(timeout=5)
            run.exit_code = proc.returncode
            run.timed_out = fired.is_set()
            run.left_in_folder = sorted(os.listdir(folder))[:20]
            errors.seek(max(0, errors.seek(0, os.SEEK_END) - 20_000))  # its last words matter most
            run.stderr = errors.read().decode("utf-8", "replace")
        return run

    def _reply(self, run: _Run, digest: str, seconds: float) -> Reply:
        if run.stopped:
            kind, reason = run.stopped
            raise kind(reason)
        if run.timed_out:  # whatever it printed before: a call that passes its deadline fails
            raise ModelTimeout(f"no reply within {self.timeout_seconds:g} s; Claude Code was stopped")
        result = next((e for e in reversed(run.events) if e.get("type") == "result"), None)
        if result is None:
            raise ModelError(
                f"Claude Code stopped without a result (exit code {run.exit_code}){_said(run.stderr[-2000:], last=True)}"
            )
        if result.get("is_error") is not False or result.get("subtype") != "success":
            subtype = clean_text(str(result.get("subtype")), 60)
            raise ModelError(
                f"Claude Code reported an error ({subtype}){_said(result.get('result') or result.get('errors'))}"
            )
        if run.exit_code != 0 and not run.lingered:  # a reply complete before Klaxon stopped it still counts
            raise ModelError(f"Claude Code exited with code {run.exit_code}{_said(run.stderr[-2000:], last=True)}")
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
            received_at=utc_now(),
            cost_usd=finite(result.get("total_cost_usd")),
            raw=_home_as_tilde(run.events),
        )

    def _details(self, system: str, stdin: bytes, run: _Run) -> dict:
        """How the call was made, for the recording: everything but the prompt, which is stored by part."""
        init = next((e for e in run.events if _is_init(e)), {})
        shown = command(self.binary, self.model, f"<the system prompt, sha256 {text_sha256(system)}>")
        return {
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
                }
            ),
            "command": [clean_text(arg, 300) for arg in shown],
            "environment": sorted(environment()),
            "working_folder": "a new empty temporary folder, removed after the call",
            "left_in_folder": [clean_text(name, 100) for name in run.left_in_folder],
            "stdin_sha256": hashlib.sha256(stdin).hexdigest(),
            "stdin_bytes": len(stdin),
            "timeout_seconds": self.timeout_seconds,
            "exit_code": run.exit_code,
            "timed_out": run.timed_out,
            "stopped_after_its_reply": run.lingered,
            "unreadable_lines": run.unreadable_lines,
            "stderr": clean_text(run.stderr[-2000:], 2000),
        }


# The live check: one tiny call, to show headless mode works on the seat.

CHECK_SYSTEM = "You are answering a connectivity check for a software project. Follow the user's instruction exactly."
CHECK_PROMPT = "Reply with the word ok"


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
        print(
            f"Environment: {', '.join(sorted(PASSED_ON))} when set, and {', '.join(f'{k}={v}' for k, v in FIXED.items())}"
        )
        print(f"Timeout: {DEFAULT_TIMEOUT:.0f} s, then the whole process group is killed")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    path = args.evidence_dir / f"{stamp}_claude-check.jsonl"
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
    return 0 if reply.text.strip().strip(".!").lower() == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
