"""Small pieces every service uses: settings from the environment, JSON logs, a heartbeat.

Logs are one JSON object per line on stdout, so Docker keeps them and a log
search (week 3) can filter by field. Inside a trace, a line also carries its
trace_id and span_id, so it leads to the trace (week 2, see telemetry). Klaxon
will read these logs, so they are untrusted input (prompt-injection text can
hide in them). To keep that surface small, request lines record the route
template ("/payments/{payment_id}"), never the raw path a caller sent, and every
logged string is cut at 300 characters.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opentelemetry import trace


def env_str(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"missing required setting {name}")
    return value


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def env_list(name: str, default: str | None = None) -> list[str]:
    return [item.strip() for item in env_str(name, default).split(",") if item.strip()]


MAX_LOGGED_CHARS = 300


def capped(value: object) -> object:
    if isinstance(value, str) and len(value) > MAX_LOGGED_CHARS:
        return f"{value[:MAX_LOGGED_CHARS]}...[{len(value) - MAX_LOGGED_CHARS} more characters cut]"
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": capped(record.getMessage()),
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            line["trace_id"] = format(span.trace_id, "032x")
            line["span_id"] = format(span.span_id, "016x")
        line.update({name: capped(value) for name, value in getattr(record, "fields", {}).items()})
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)[-2000:]  # the end of a traceback says the most
        return json.dumps(line, default=str)


def configure_logging(service: str, level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    # httpx logs every request at INFO with its full URL, and psycopg's pool every connection it hands out;
    # our own lines already say what happened.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("psycopg.pool").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    """Log with structured fields, for example log(logger, logging.INFO, "posted", event_id="evt_1")."""
    logger.log(level, msg, extra={"fields": fields})


def _route(request: Any) -> str:
    """The matched route's template, never the raw path (which the caller chose)."""
    return getattr(request.scope.get("route"), "path", None) or "(no matching route)"


def add_request_logging(app: Any, logger: logging.Logger) -> None:
    """One log line per HTTP request: method, route, status and duration. Health checks are skipped."""

    @app.middleware("http")
    async def _log_request(request: Any, call_next: Any) -> Any:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled error", extra={"fields": {"method": request.method, "route": _route(request)}})
            raise
        route = _route(request)
        if route != "/healthz":
            log(
                logger,
                logging.INFO,
                "request",
                method=request.method,
                route=route,
                status=response.status_code,
                ms=round((time.perf_counter() - start) * 1000, 1),
            )
        return response


def add_trusted_hosts(app: Any, *service_names: str) -> None:
    """Answer only requests addressed to this machine or to the service's Compose name.

    The ports listen on 127.0.0.1 only, but a web page can still aim a browser at
    127.0.0.1 through DNS rebinding. Such a request carries the attacker's host name
    in its Host header, so it is refused (400).
    """
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    allowed = ["127.0.0.1", "localhost", "testserver", *service_names]  # testserver: FastAPI's test client
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)


class Heartbeat:
    """A file the service touches while its main loop runs. The container health check reads its age."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self._last = 0.0

    def beat(self) -> None:
        now = time.monotonic()
        if self.path is not None and now - self._last >= 1.0:
            self.path.touch()
            self._last = now
