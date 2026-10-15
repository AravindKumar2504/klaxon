"""Settings of the observability container that Klaxon relies on, read from the files the stack mounts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_related_logs_search_a_minute_either_side_of_the_span():
    """Without the shifts, Grafana searches the span's own milliseconds and misses a consumer's outcome line."""
    sources = (ROOT / "docker/grafana/datasources.yaml").read_text()
    tempo = sources.split("- name: Tempo")[1].split("- name: Loki")[0]
    assert 'spanStartTimeShift: "-1m"' in tempo and 'spanEndTimeShift: "1m"' in tempo
    assert 'trace_id = "$${__trace.traceId}"' in tempo  # the wider window keeps to this trace's lines
    compose = (ROOT / "compose.yaml").read_text()
    assert "./docker/grafana/datasources.yaml:/otel-lgtm/grafana/conf/provisioning/datasources/" in compose


def test_each_consumer_takes_its_bad_event_policy_from_its_own_setting_with_dead_lettering_by_default():
    """The poison-message fault redeploys the ledger with LEDGER_BAD_EVENTS=retry; nothing else may change."""
    compose = (ROOT / "compose.yaml").read_text()
    ledger = compose.split("  ledger-consumer:")[1].split("\n  payment-confirmer:")[0]
    confirmer = compose.split("  payment-confirmer:")[1].split("\n  otel-lgtm:")[0]
    assert "BAD_EVENTS: ${LEDGER_BAD_EVENTS:-dead_letter}" in ledger
    assert "BAD_EVENTS: ${CONFIRMER_BAD_EVENTS:-dead_letter}" in confirmer


def test_the_dashboard_shows_provider_calls_that_got_no_answer_from_our_own_count():
    """The httpx instrumentation records no duration for a call that timed out, so its metric never shows one."""
    import json

    dashboard = json.loads((ROOT / "docker/grafana/klaxon-patient.json").read_text())
    exprs = [
        target["expr"]
        for row in dashboard["panels"]
        for panel in [row, *row.get("panels", [])]
        for target in panel.get("targets", [])
        if "expr" in target
    ]
    assert 'sum by (error) (rate(klaxon_provider_attempts_total{result="no_answer"}[1m]))' in exprs
    assert not any('http_response_status_code=""' in expr for expr in exprs)
