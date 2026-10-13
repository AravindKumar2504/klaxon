"""The services' tests run with telemetry on, as the services do, but sent to memory instead of grafana/otel-lgtm.

OpenTelemetry lets a process set its providers once, so one set serves the whole test session; each test starts
from cleared spans and logs. Counters keep their totals across tests: compare a value before and after.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from klaxon.patient.services.telemetry import build, install


@dataclass
class Capture:
    spans: InMemorySpanExporter
    reader: InMemoryMetricReader
    logs: InMemoryLogRecordExporter

    def finished(self, name: str | None = None, kind: SpanKind | None = None) -> list[ReadableSpan]:
        return [
            span
            for span in self.spans.get_finished_spans()
            if (name is None or span.name == name) and (kind is None or span.kind is kind)
        ]

    def points(self, name: str) -> list:
        """Every data point of this metric so far, one per set of attributes."""
        data = self.reader.get_metrics_data()
        return [
            point
            for resource in (data.resource_metrics if data else [])
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]

    def counter(self, name: str, **attributes: str) -> int:
        """A counter's total so far for exactly these attributes (0 before its first count)."""
        for point in self.points(name):
            if dict(point.attributes) == attributes:
                return point.value
        return 0

    def log_records(self) -> list:
        return [record.log_record for record in self.logs.get_finished_logs()]


@pytest.fixture(scope="session")
def telemetry_session() -> Capture:
    capture = Capture(InMemorySpanExporter(), InMemoryMetricReader(), InMemoryLogRecordExporter())
    install(build("tests", capture.spans, capture.reader, capture.logs, batch=False))
    return capture


@pytest.fixture(autouse=True)
def telemetry(telemetry_session: Capture) -> Capture:
    telemetry_session.spans.clear()
    telemetry_session.logs.clear()
    return telemetry_session
