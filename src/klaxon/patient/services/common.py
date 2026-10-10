"""Small pieces every service uses: settings from the environment, JSON logs, a heartbeat.

Logs are one JSON object per line on stdout, so Docker keeps them and a log
search (week 3) can filter by field. Treat log content as untrusted input:
anything that reached a request body can end up in a log line.
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
            "msg": record.getMessage(),
        }
        line.update(getattr(record, "fields", {}))
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging(service: str, level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))


def log(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    """Log with structured fields, for example log(logger, logging.INFO, "posted", event_id="evt_1")."""
    logger.log(level, msg, extra={"fields": fields})


def add_request_logging(app: Any, logger: logging.Logger) -> None:
    """One log line per HTTP request: method, path, status and duration. Health checks are skipped."""

    @app.middleware("http")
    async def _log_request(request: Any, call_next: Any) -> Any:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled error", extra={"fields": {"method": request.method, "path": request.url.path}})
            raise
        if request.url.path != "/healthz":
            log(
                logger,
                logging.INFO,
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=round((time.perf_counter() - start) * 1000, 1),
            )
        return response


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
