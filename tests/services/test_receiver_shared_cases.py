"""The shared table of webhook cases, run against the Python receiver (week 8, item 2).

go/receiver/testdata/shared_cases.json holds the cases; the Go receiver's shared_cases_test.go runs the same file. Both
must give each case the same status, body and headers, count the same result, and write the same message to Kafka.
The Python receiver is the reference: if it changes how it answers, its change shows here first.

Each request goes to the app as the server hands it over (an ASGI call), not through FastAPI's test client: that
client sends a header's raw bytes re-encoded as UTF-8, and some cases send bytes that are not ASCII.
"""

import asyncio
import json
import re
from urllib.parse import unquote

import pytest

from klaxon.patient import signatures
from klaxon.patient.services.kafka_io import EVENTS_TOPIC, PublishError
from klaxon.patient.services.receiver_app import create_app, secrets_from_env
from klaxon.patient.stack import REPO
from tests.services.fakes import FakePublisher

TABLE = json.loads((REPO / "go" / "receiver" / "testdata" / "shared_cases.json").read_text(encoding="utf-8"))
CASES = TABLE["cases"]
RESULTS = ("queued", "bad_signature", "bad_payload", "too_large", "queue_unavailable")
RECEIVED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00")  # UTC, to the millisecond


def body_of(request: dict) -> bytes:
    if "body_repeat" in request:
        return request["body_repeat"]["text"].encode() * request["body_repeat"]["times"]
    if "body_latin1" in request:
        return request["body_latin1"].encode("latin-1")
    return request.get("body", "").encode()


def headers_of(request: dict, body: bytes) -> list[tuple[bytes, bytes]]:
    headers = [(b"host", request.get("host", "testserver").encode())]
    if not request.get("chunked"):  # chunked: no length, the body arrives in pieces
        headers.append((b"content-length", str(len(body)).encode()))
    if "signature_latin1" in request:
        headers.append((b"klaxon-signature", request["signature_latin1"].encode("latin-1")))  # raw bytes
    elif "signature" in request:
        headers.append((b"klaxon-signature", request["signature"].encode()))
    return headers


def call(app, request: dict) -> tuple[int, dict[str, str], bytes]:
    """One request, as uvicorn hands it to the app: its status, headers and body."""
    body = body_of(request)
    path = request.get("path", "/webhooks/provider")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": request.get("method", "POST"),
        "scheme": "http",
        "path": unquote(path),
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers_of(request, body),
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    size = 1024 if request.get("chunked") else max(len(body), 1)
    pieces = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
    incoming = [{"type": "http.request", "body": piece, "more_body": i < len(pieces) - 1} for i, piece in enumerate(pieces)]
    answer: dict = {"body": b""}

    async def receive() -> dict:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            answer["status"] = message["status"]
            answer["headers"] = {name.decode(): value.decode() for name, value in message["headers"]}
        elif message["type"] == "http.response.body":
            answer["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return answer["status"], answer["headers"], answer["body"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_python_receiver_answers_each_shared_case(case, telemetry):
    environ = TABLE["secrets"] | {"WEBHOOK_SECRET_VERSION": case["secret_version"]}
    down = case["kafka"] == "down"
    publisher = FakePublisher(fail=PublishError("no acknowledgement from Kafka in time") if down else None)
    publisher.is_healthy = not down
    app = create_app(publisher, secrets_from_env(environ), now=lambda: TABLE["now"])
    request, expect = case["request"], case["expect"]

    def counts() -> list[int]:
        return [telemetry.counter("klaxon.webhooks.received", result=result) for result in RESULTS]

    before = counts()
    status, headers, body = call(app, request)

    assert status == expect["status"]
    assert body == expect["body"].encode()
    for name, value in expect["headers"].items():
        assert headers[name] == value
    counted = [after - first for first, after in zip(before, counts(), strict=True)]
    assert counted == [int(result == expect["result"]) for result in RESULTS]
    if expect["kafka_key"] is None:
        assert publisher.messages == []
        return
    [message] = publisher.messages
    assert (message["topic"], message["key"], message["value"]) == (EVENTS_TOPIC, expect["kafka_key"], body_of(request))
    assert list(message["headers"]) == TABLE["kafka_headers"]
    assert message["headers"]["content-type"] == "application/json"
    assert RECEIVED_AT.fullmatch(message["headers"]["received-at"])


@pytest.mark.parametrize("case", [c for c in CASES if "signed_with" in c["request"]], ids=lambda c: c["name"])
def test_each_signature_in_the_table_is_the_one_the_provider_makes(case):
    """A body edited by hand needs its signature made again: this says which one."""
    request = case["request"]
    secret, timestamp = request["signed_with"]["secret"], request["signed_with"]["timestamp"]
    assert request["signature"] == signatures.sign(secret.encode(), timestamp, body_of(request))


def test_the_table_has_the_plans_cases_and_every_result():
    names = {case["name"] for case in CASES}
    for planned in (
        "a good signature",
        "a wrong secret",
        "an old timestamp, 301 s before now",
        "a malformed signature header",
        "a body too large: 64 KiB and one byte",
        "a bad payload: not JSON",
        "Kafka down",
        "a rotation, v1,v2: a v2 signature",
    ):
        assert planned in names
    assert {case["expect"]["result"] for case in CASES} == {*RESULTS, None}
    assert TABLE["secrets"]["WEBHOOK_SECRET_V1"].endswith("-not-real")  # made up, never a real secret
