"""OpenTelemetry for the patient services: traces, metrics and logs, sent to grafana/otel-lgtm (week 2).

What the services send:
- Traces. Every HTTP request our APIs answer (FastAPI's own server spans), every call the payments API
  makes to the provider (httpx), and every database query (psycopg) and Redis command. A webhook's trip
  through Kafka stays one trace: the receiver puts the trace context in the message's headers, and both
  consumers continue it, so a posting and a confirmation sit under the webhook that brought them.
- Metrics. FastAPI's request counts and durations (by route and status code), the durations of the
  provider calls (httpx), and our own: webhook results, consumer outcomes, retries and dead letters, each
  consumer group's lag, and the payments still pending.
- Logs. The lines each service prints as JSON, with the same fields and the trace id.

The fake provider sends none of this. It stands for an outside company, whose internals we would not see.
Its webhooks start new traces at our receiver; payment and event ids tie them to the API's traces.

Untrusted input. Klaxon will read all of this, and text a caller chose can carry instructions (prompt
injection). So:
- Trace context from outside is ignored. A caller's traceparent, tracestate and baggage headers could
  switch tracing off, pick our trace ids or put its own text on every span; our services start their own
  traces instead, and send no trace context to the provider either. Only our own hop through Kafka carries
  it, and only the traceparent (see kafka_trace_headers and trace_from_kafka).
- Server spans never keep the raw path, the query, the original method or the Host header: they are
  overwritten when the span starts, and the route template stays.
- Database and Redis spans record statements without their values, and every string attribute is cut at
  300 characters, as in the JSON logs (a stack trace keeps its last 2,000).
- FastAPI's own log records are off: our request logging already reports every error, capped.

Health checks are left out of traces and request metrics: Docker calls them every 10 seconds.

Without OTEL_EXPORTER_OTLP_ENDPOINT, setup() does nothing, and a service runs as it did in week 1. The SDK
sends whatever is still buffered when the process exits.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from opentelemetry import _logs, metrics, propagate, trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.context import Context
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.sdk._logs import LoggerProvider, LogRecordLimits
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span, SpanLimits, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from klaxon.patient.services.common import MAX_LOGGED_CHARS, capped

NAMESPACE = "klaxon-patient"
# Attributes of a server span whose value the caller chose. The route template ("/payments/{payment_id}")
# and the normalized method stay; these are overwritten.
CALLER_CHOSEN = ("url.path", "url.query", "http.request.method_original", "server.address")
REDACTED = "redacted"
MAX_STACK_CHARS = 2000  # the end of a traceback says the most


def is_health_check(scope: dict[str, Any]) -> bool:
    return scope.get("path") == "/healthz"


# FastAPI's native telemetry, for our two APIs. Our setup() makes the providers (FastAPI must not add
# exporters of its own); no operation spans (one span per request is enough); no FastAPI log records.
FASTAPI = {"auto_configure": False, "operation_spans": False, "logs": False, "exclude": is_health_check}

_SEVERITY = {
    logging.DEBUG: SeverityNumber.DEBUG,
    logging.INFO: SeverityNumber.INFO,
    logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR,
    logging.CRITICAL: SeverityNumber.FATAL,
}


class IgnoreOutsideContext(TextMapPropagator):
    """The process-wide propagator: take no trace context from requests, put none on outgoing ones."""

    def extract(
        self, carrier: CarrierT, context: Context | None = None, getter: Getter[CarrierT] = default_getter
    ) -> Context:
        return context if context is not None else Context()

    def inject(
        self, carrier: CarrierT, context: Context | None = None, setter: Setter[CarrierT] = default_setter
    ) -> None:
        return None

    @property
    def fields(self) -> set[str]:
        return set()


_W3C = TraceContextTextMapPropagator()  # for our own hop through Kafka only


class _TraceparentOnly(Getter[list]):
    """Read only the traceparent from a Kafka message's headers, a list of (name, bytes): never tracestate or
    baggage, which can hold anyone's text. Kafka is still a trusted zone (STATUS, known gap 2)."""

    def get(self, carrier: list, key: str) -> list[str] | None:
        if key != "traceparent":
            return None
        values = [value.decode("latin-1") for name, value in carrier if name == key and value is not None]
        return values[:1] or None

    def keys(self, carrier: list) -> list[str]:
        return ["traceparent"]


_TRACEPARENT_ONLY = _TraceparentOnly()


def kafka_trace_headers() -> dict[str, str]:
    """The current trace, as Kafka message headers: the traceparent only."""
    carrier: dict[str, str] = {}
    _W3C.inject(carrier)
    return {"traceparent": carrier["traceparent"]} if "traceparent" in carrier else {}


def trace_from_kafka(headers: list | None) -> Context:
    """The trace a Kafka message continues, from its traceparent header (an empty context when it has none)."""
    return _W3C.extract(headers or [], getter=_TRACEPARENT_ONLY)


class RedactCallerText(SpanProcessor):
    """Overwrite the caller-chosen attributes of every server span, as it starts."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if span.kind is not SpanKind.SERVER or not span.attributes:
            return
        for key in CALLER_CHOSEN:
            if key in span.attributes:
                span.set_attribute(key, REDACTED)


def attribute_value(value: object) -> str | bool | int | float | list | None:
    """A log field as an OpenTelemetry attribute: plain values kept, strings capped, the rest as capped text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return capped(value)
    if isinstance(value, (list, tuple)) and all(isinstance(v, (bool, int, float)) for v in value):
        return list(value)
    return capped(str(value))


class OtlpLogHandler(logging.Handler):
    """Send each log line to the logs pipeline, with the fields the JSON line has. The SDK adds the current trace."""

    def __init__(self, provider: LoggerProvider) -> None:
        super().__init__()
        self.provider = provider

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("opentelemetry"):
            return  # the SDK's own lines (an export that failed, say) stay on stdout: sending them would loop
        try:
            attributes: dict[str, object] = {"logger": record.name}
            for name, value in getattr(record, "fields", {}).items():
                value = attribute_value(value)
                if value is not None:
                    attributes[name] = value
            if record.exc_info and record.exc_info[0] is not None:
                attributes["exception.type"] = record.exc_info[0].__name__
                attributes["exception.stacktrace"] = logging.Formatter().formatException(record.exc_info)[
                    -MAX_STACK_CHARS:
                ]
            self.provider.get_logger(record.name).emit(
                timestamp=int(record.created * 1e9),
                severity_number=_SEVERITY.get(record.levelno, SeverityNumber.UNSPECIFIED),
                severity_text=record.levelname,
                body=capped(record.getMessage()),
                attributes=attributes,
            )
        except Exception:
            self.handleError(record)


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider


def build(
    service: str, spans: SpanExporter, metric_reader: MetricReader, logs: LogRecordExporter, batch: bool = True
) -> Telemetry:
    """The three providers for one service, sending to these exporters. batch=False sends at once (tests)."""
    resource = Resource.create(
        {"service.name": service, "service.namespace": NAMESPACE, "deployment.environment.name": "local"}
    )
    tracer_provider = TracerProvider(resource=resource, span_limits=SpanLimits(max_attribute_length=MAX_LOGGED_CHARS))
    tracer_provider.add_span_processor(RedactCallerText())
    tracer_provider.add_span_processor(BatchSpanProcessor(spans) if batch else SimpleSpanProcessor(spans))
    meter_provider = MeterProvider(metric_readers=[metric_reader], resource=resource)
    # A backstop: OtlpLogHandler caps every value itself.
    logger_provider = LoggerProvider(
        resource=resource, log_record_limits=LogRecordLimits(max_attribute_length=MAX_STACK_CHARS)
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(logs) if batch else SimpleLogRecordProcessor(logs))
    return Telemetry(tracer_provider, meter_provider, logger_provider)


def install(telemetry: Telemetry) -> None:
    """Make these the process's providers (OpenTelemetry allows that once per process) and forward logs."""
    propagate.set_global_textmap(IgnoreOutsideContext())  # trace context from outside is ignored
    trace.set_tracer_provider(telemetry.tracer_provider)
    metrics.set_meter_provider(telemetry.meter_provider)
    _logs.set_logger_provider(telemetry.logger_provider)
    logging.getLogger().addHandler(OtlpLogHandler(telemetry.logger_provider))


def setup(service: str) -> Telemetry | None:
    """Send this service's telemetry to OTEL_EXPORTER_OTLP_ENDPOINT. Call after configure_logging, before
    any database pool, Redis or httpx client is made, so they are instrumented. None when not configured."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return None
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    # The exporters read the endpoint, protocol and export interval from the standard OTEL_* variables.
    telemetry = build(
        service, OTLPSpanExporter(), PeriodicExportingMetricReader(OTLPMetricExporter()), OTLPLogExporter()
    )
    install(telemetry)
    PsycopgInstrumentor().instrument()
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    return telemetry
