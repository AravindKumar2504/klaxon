"""OpenHands' own agent on one saved task, in its own Docker sandbox, on a local model (week 8, item 4; CSYE 7374 only).

Run by klaxon.openhands_task in OpenHands' own venv (.venv-openhands: make openhands-venv), never by Klaxon's tests:

    .venv-openhands/bin/python experiments/openhands/run_openhands.py --data DIR --task FILE --out DIR

The sandbox is the publisher's agent-server image. The saved files are mounted read-only. The agent gets only the
tools that find, search and view files: glob, grep, and the planning editor, which writes nothing but its own PLAN.md
inside the sandbox. A hook refuses any other tool, should one be offered. There is no terminal. Its model is qwen3:8b
in Ollama on this Mac, reached from the sandbox at host.docker.internal (Ollama listens on this Mac only). Each event
is saved as it comes, as OpenHands' own record of each step; then the agent's last message, as it wrote it, and a
summary of how the run ended.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SDK = "1.60.1"
IMAGE = f"ghcr.io/openhands/agent-server:{SDK}-python"
MODEL = "qwen3:8b"
OLLAMA_FROM_SANDBOX = "http://host.docker.internal:11434/v1"
MOUNT = "/workspace/project/data"  # below /workspace, never over it: the server writes its own files there
TOOLS = ("glob", "grep", "planning_file_editor")
ALLOWED = (*TOOLS, "finish")  # and the SDK's own one: end the run with a message
EVENTS, ANSWER, SUMMARY = "openhands-events.jsonl", "openhands-answer.txt", "openhands-summary.json"
# Runs in the sandbox, as the SDK runs every command hook: the event, as JSON, on its input. Exit 2 refuses the tool;
# the SDK lets any other non-zero exit through, so every failure here (no python3, input it cannot read) becomes 2.
DENY_OTHERS = (
    "python3 -c 'import json, sys; event = json.load(sys.stdin); "
    'sys.exit(0 if isinstance(event, dict) and event.get("tool_name") in '
    f"{json.dumps(sorted(ALLOWED))} else 2)' || exit 2"
)


def text_of(event: dict) -> tuple[str | None, str | None]:
    """The agent's words in one event, as saved: a finish action's message, or a message the agent sent. And which."""
    action = event.get("action") or {}
    if event.get("tool_name") == "finish" and isinstance(action, dict) and isinstance(action.get("message"), str):
        return action["message"], "its finish action"
    if event.get("source") == "agent" and isinstance(event.get("llm_message"), dict):
        parts = event["llm_message"].get("content") or []
        words = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
        return (words, "a message it sent") if words.strip() else (None, None)
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, required=True, help="the saved files, mounted read-only")
    parser.add_argument("--task", type=Path, required=True, help="the task, as Klaxon wrote it")
    parser.add_argument("--out", type=Path, required=True, help="where the record of each step goes")
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=900, help="seconds")
    args = parser.parse_args()
    data, out = args.data.resolve(), args.out.resolve()
    if not data.is_dir() or not args.task.is_file():
        print(f"Not started: {data} is not a folder, or {args.task} is not a file", file=sys.stderr)
        return 2

    from openhands.sdk import LLM, Agent, Conversation, Tool
    from openhands.sdk.hooks import HookConfig, HookDefinition, HookMatcher
    from openhands.workspace import DockerWorkspace

    llm = LLM(
        model=f"openai/{MODEL}",  # Ollama's OpenAI-compatible API
        base_url=OLLAMA_FROM_SANDBOX,
        api_key="ollama-on-this-mac",  # Ollama asks for none; the client wants one
        reasoning_effort="none",
        temperature=0.0,
        timeout=300,
        num_retries=1,
        drop_params=True,
        usage_id="agent",
    )
    agent = Agent(llm=llm, tools=[Tool(name=name) for name in TOOLS])
    hooks = HookConfig(pre_tool_use=[HookMatcher(matcher="*", hooks=[HookDefinition(command=DENY_OTHERS)])])
    last: dict = {"text": None, "from": None}
    counted = {"events": 0}
    errors: list[dict] = []
    started = time.monotonic()
    outcome, status = "finished", None
    with (out / EVENTS).open("w") as events:

        def keep(event) -> None:  # OpenHands' own record of each step, as it comes
            saved = json.loads(event.model_dump_json())
            events.write(json.dumps(saved, ensure_ascii=False) + "\n")
            events.flush()
            counted["events"] += 1
            words, said = text_of(saved)
            if words is not None:
                last["text"], last["from"] = words, said
            if (
                isinstance(saved.get("code"), str) and "detail" in saved
            ):  # how a run ends early: MaxIterationsReached...
                errors.append({"code": saved["code"], "detail": str(saved["detail"])[:300]})

        with DockerWorkspace(server_image=IMAGE, platform="linux/arm64", volumes=[f"{data}:{MOUNT}:ro"]) as workspace:
            conversation = Conversation(
                agent=agent,
                workspace=workspace,
                callbacks=[keep],
                hook_config=hooks,
                max_iteration_per_run=args.max_iterations,
                visualizer=None,
            )
            conversation.send_message(args.task.read_text())
            try:
                conversation.run(timeout=args.timeout)
            except Exception as exc:  # a time limit, the model, the sandbox: said, and the record kept
                outcome = f"error: {type(exc).__name__}: {str(exc)[:400]}"
            try:
                status = str(conversation.state.execution_status)
            except Exception as exc:
                status = f"unknown ({type(exc).__name__})"
    (out / ANSWER).write_text(last["text"] or "")
    summary = {
        "sdk": SDK,
        "image": IMAGE,
        "platform": "linux/arm64",
        "model": MODEL,
        "tools": list(TOOLS),
        "mount": f"{MOUNT}:ro",
        "max_iterations": args.max_iterations,
        "timeout_seconds": args.timeout,
        "outcome": outcome,
        "execution_status": status,
        "errors": errors,
        "events": counted["events"],
        "answer_from": last["from"],
        "seconds": round(time.monotonic() - started, 1),
    }
    (out / SUMMARY).write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary))
    return 0 if last["text"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
