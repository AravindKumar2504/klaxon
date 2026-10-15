"""Klaxon's read-only tools against the running stack (make up). They only read: nothing here changes the stack.

What a tool finds depends on the traffic so far, so these check each source's real answers and refusals.
The end-to-end suite checks what the tools find after a payment, on a fresh stack.
"""

import json
import os
import sys
import time

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from klaxon.agent.tools import ToolInputError, make_tools
from klaxon.patient.stack import REPO

from .conftest import PASSWORDS

pytestmark = pytest.mark.integration

LONG_RUNNING = {
    "fake-provider",
    "kafka",
    "ledger-consumer",
    "otel-lgtm",
    "payment-confirmer",
    "payments-api",
    "postgres",
    "redis",
    "webhook-receiver",
}


@pytest.fixture(scope="module")
def tools():
    return make_tools()


def eventually(probe, seconds: float = 30.0):
    """The services export metrics every 10 s, so a stack just started may need a moment."""
    deadline = time.monotonic() + seconds
    while not (value := probe()):
        assert time.monotonic() < deadline, "nothing arrived in time"
        time.sleep(1.0)
    return value


def test_the_patients_own_metrics_are_listed_with_their_labels(tools):
    out = eventually(lambda: (o := tools["list_metrics"].call({"match": "klaxon_"})) and o["metrics"] and o)
    by_name = {m["name"]: m.get("labels") for m in out["metrics"]}
    assert "consumer" in by_name["klaxon_kafka_consumer_lag"] and "partition" in by_name["klaxon_kafka_consumer_lag"]
    assert all("instance" not in labels and "job" not in labels for labels in by_name.values())


def test_a_promql_query_comes_back_per_consumer(tools):
    query = "sum by (consumer) (klaxon_kafka_consumer_lag)"
    out = eventually(lambda: (o := tools["query_metrics"].call({"query": query})) and len(o["series"]) == 2 and o)
    assert {s["labels"]["consumer"] for s in out["series"]} == {"ledger", "payment-confirmer"}
    ranged = tools["query_metrics"].call({"query": query, "minutes": 10})
    assert all(len(s["points"]) <= 30 for s in ranged["series"])


def test_each_source_refuses_a_broken_query_in_its_own_words(tools):
    with pytest.raises(ToolInputError, match="Prometheus refused the query"):
        tools["query_metrics"].call({"query": "sum(rate(("})
    with pytest.raises(ToolInputError, match="Loki refused the query"):
        tools["search_logs"].call({"query": "{service_name="})
    with pytest.raises(ToolInputError, match="Tempo refused the query"):
        tools["search_traces"].call({"query": "{ nope"})
    with pytest.raises(ToolInputError, match="Tempo has no spans for trace 0123456789abcdef"):
        tools["get_trace"].call({"trace_id": "0123456789abcdef0123456789abcdef"})


def test_logs_and_traces_come_back_in_the_tools_shape(tools):
    logs = tools["search_logs"].call({"query": '{service_name=~".+"}', "minutes": 120, "limit": 5})
    assert set(logs) == {"query", "from", "to", "lines", "more_may_exist"}
    assert all(line["service"] in LONG_RUNNING for line in logs["lines"])
    traces = tools["search_traces"].call({"query": "{}", "minutes": 120, "limit": 3})
    assert set(traces) == {"query", "from", "to", "traces", "more_may_exist"}


def test_every_long_running_container_is_up_and_healthy(tools):
    out = tools["service_health"].call({})
    by_name = {s["service"]: s for s in out["services"]}
    assert {name for name in by_name if by_name[name]["state"] == "running"} == LONG_RUNNING
    assert all(by_name[name]["health"] == "healthy" for name in LONG_RUNNING)
    assert (by_name["kafka-init"]["state"], by_name["kafka-init"]["exit_code"]) == ("exited", 0)


def test_recent_deploys_shows_behavior_settings_and_no_password_from_the_real_containers(tools):
    out = tools["recent_deploys"].call({})
    settings = {d["service"]: d["settings"] for d in out["deploys"]}
    assert set(settings["ledger-consumer"]) == {"LEDGER_HANDLER", "BAD_EVENTS"}
    assert set(settings["payment-confirmer"]) == {"CONFIRMER_HANDLER", "BAD_EVENTS"}
    text = json.dumps(out)
    # The real containers' DATABASE_URLs hold these local development passwords; none may leave the tool.
    assert not [role for role, password in PASSWORDS.items() if password in text]
    assert "DATABASE_URL" not in text and "postgresql://" not in text


def test_the_money_check_runs_through_the_read_only_role(tools):
    out = tools["money_check"].call({"wait": False})
    assert out["outcome"] in ("passed", "failed")
    assert [i["name"] for i in out["invariants"]] == [
        "posted_exactly_once",
        "ledger_balanced",
        "matches_provider",
        "payments_match_provider",
        "refunds_match_provider",
    ]


def test_the_official_sdk_client_calls_a_tool_on_the_real_stack():
    # PYTHONPATH too: on a mutant's copy (scripts/mutation_check.py) the server must run the code under test.
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "klaxon.agent.mcp_server"],
        cwd=str(REPO),
        env={k: v for k, v in os.environ.items() if k == "PYTHONPATH"},
    )

    async def talk():
        async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("service_health", {})

    result = anyio.run(talk)
    assert not result.is_error
    assert {s["service"] for s in result.structured_content["services"]} >= LONG_RUNNING
