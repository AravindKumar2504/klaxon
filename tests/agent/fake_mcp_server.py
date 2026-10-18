"""A fake MCP server for the client's tests: one line of JSON-RPC per message over stdio, and one way to misbehave.

Run as: python fake_mcp_server.py MODE [FILE]. MODE "good" answers like Klaxon's server, with one made-up tool;
every other mode breaks one rule (see BEHAVIORS). FILE is where a mode writes what the test needs to know (the
pid of a child it started). It imports nothing of Klaxon's, so it starts fast. Nothing here is real data.
"""

import json
import subprocess
import sys
import time

MODE = sys.argv[1]
FILE = sys.argv[2] if len(sys.argv) > 2 else None
TOOL = {
    "name": "service_health",
    "title": "Service health",
    "description": "Made up for a test.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
}
RESULT = {"content": [{"type": "text", "text": '{"services": []}'}], "isError": False}


def write(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def answer(request, result):
    write({"jsonrpc": "2.0", "id": request["id"], "result": result})


def initialize(request):
    if MODE == "silent":
        time.sleep(3)
        sys.exit(0)
    if MODE == "stderr-flood":
        for _ in range(40):
            sys.stderr.write("noise " * 5000 + "\n")
        sys.stderr.flush()
    version = "2024-11-05" if MODE == "wrong-version" else "2025-11-25"
    name = "someone-else" if MODE == "wrong-name" else "klaxon-ops"
    capabilities = {} if MODE == "no-tools-capability" else {"tools": {}}
    result = {"protocolVersion": version, "capabilities": capabilities, "serverInfo": {"name": name, "version": "0"}}
    if MODE == "true-id":  # initialize is request 1, and in Python True == 1: only JSON's types tell them apart
        write({"jsonrpc": "2.0", "id": True, "result": result})
        return
    answer(request, result)


def tools_list(request):
    result = {"tools": [TOOL]}
    if MODE == "paged":
        result["nextCursor"] = "page-2"
    answer(request, result)


def tools_call(request):
    if FILE:
        with open(FILE, "a") as out:
            out.write(json.dumps(request["params"]) + "\n")
    if MODE == "slow":
        time.sleep(3)
    elif MODE == "exit-on-call":
        sys.stderr.write("fake server: giving up on purpose\n")
        sys.stderr.flush()
        sys.exit(3)
    elif MODE == "garbage-on-call":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        return
    elif MODE == "huge-on-call":
        sys.stdout.write("x" * 5000)  # no newline yet: the client must give up before it ends
        sys.stdout.flush()
        time.sleep(3)
        return
    elif MODE == "wrong-id":
        write({"jsonrpc": "2.0", "id": request["id"] + 1, "result": RESULT})
        return
    elif MODE == "notify-first":
        write({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
        sys.stdout.write("\n")
    elif MODE == "bad-is-error":
        answer(request, {"content": [{"type": "text", "text": "x"}], "isError": "yes"})
        return
    elif MODE == "chatty":
        for n in range(150):
            write({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": n}})
    elif MODE == "exit-with-child":  # a child that keeps the server's stdout open after the server is gone
        subprocess.Popen(["/bin/sleep", "30"])
        sys.exit(3)
    elif MODE == "no-text":
        answer(request, {"content": [{"type": "image", "data": "", "mimeType": "image/png"}], "isError": False})
        return
    elif MODE == "error-answer":
        write({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32602, "message": "Unknown tool: x"}})
        return
    elif MODE == "child":
        child = subprocess.Popen(["/bin/sleep", "30"])
        with open(FILE + ".child", "w") as out:
            out.write(str(child.pid))
        time.sleep(30)
    elif MODE == "echo":
        text = json.dumps(request["params"]["arguments"], ensure_ascii=False)
        answer(request, {"content": [{"type": "text", "text": text}], "isError": False})
        return
    answer(request, RESULT)


if MODE == "stderr-then-exit":
    sys.stderr.write("fake server: starting\nfake server: cannot go on\n")
    sys.stderr.flush()
    sys.exit(1)

for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        initialize(request)
    elif method == "tools/list":
        tools_list(request)
    elif method == "tools/call":
        tools_call(request)
    elif "id" in request:
        write({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}})
    if MODE == "close-after-list" and method == "tools/list":
        sys.exit(0)
