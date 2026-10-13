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
