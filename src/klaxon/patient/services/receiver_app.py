"""Our webhook endpoint: check the provider's signature, put the event on Kafka, then answer 200.

The receiver books no money and does not dedupe. It proves the request came
from the provider (HMAC, see `klaxon.patient.signatures`) and hands the exact
signed bytes to Kafka. It answers 200 only after Kafka has acknowledged the
write, so the provider never hears "got it" for an event Kafka does not have.
If Kafka is down it answers 503 and the provider retries later. (This stack
runs one broker with one copy of each partition, so a crash of the whole
Docker VM could still lose the newest writes; production runs three brokers.)

Duplicates pass straight through on purpose. The ledger consumer drops them
with a database constraint, in the same transaction as the posting. Deduping
here, apart from the posting, would open a race where an event is marked
"seen" but never booked: a lost payment.

    POST /webhooks/provider
    GET  /healthz

Run: python -m klaxon.patient.services.receiver_app
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Protocol

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from klaxon.patient import signatures
from klaxon.patient.services.common import (
    add_request_logging,
    add_trusted_hosts,
    configure_logging,
    env_int,
    env_list,
    env_str,
    log,
)
from klaxon.patient.services.events import BadEvent, id_and_key
from klaxon.patient.services.kafka_io import EVENTS_TOPIC, KafkaPublisher, PublishError

logger = logging.getLogger("klaxon.receiver")

MAX_BODY_BYTES = 64 * 1024


class Publisher(Protocol):
    def publish(
        self, topic: str, key: str, value: bytes, headers: Mapping[str, str] | None = None
    ) -> tuple[int, int]: ...

    def healthy(self) -> bool: ...


class BodyTooLarge(Exception):
    pass


def verify_any(header: str | None, body: bytes, secrets: list[bytes], now: int, tolerance: int) -> None:
    """Accept a signature made with any of our current secrets (during a rotation, two are current)."""
    if not secrets:
        raise signatures.SignatureError("no webhook secret configured")
    error: signatures.SignatureError | None = None
    for secret in secrets:
        try:
            signatures.verify(header, body, secret, now, tolerance)
            return
        except signatures.SignatureError as exc:
            error = exc
    raise error


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise BodyTooLarge
    return bytes(body)


def create_app(
    publisher: Publisher,
    secrets: list[bytes],
    topic: str = EVENTS_TOPIC,
    tolerance_seconds: int = signatures.DEFAULT_TOLERANCE_SECONDS,
    now: Callable[[], float] = time.time,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        close = getattr(publisher, "close", None)
        if close is not None:
            close()

    app = FastAPI(title="Webhook receiver", lifespan=lifespan)
    add_request_logging(app, logger)
    add_trusted_hosts(app, "webhook-receiver")

    @app.post("/webhooks/provider")
    async def receive(request: Request) -> JSONResponse:
        try:
            body = await _read_body(request, MAX_BODY_BYTES)
        except BodyTooLarge:
            return JSONResponse({"error": "body_too_large"}, status_code=413)
        try:
            verify_any(request.headers.get(signatures.HEADER), body, secrets, int(now()), tolerance_seconds)
        except signatures.SignatureError as exc:
            log(logger, logging.WARNING, "webhook refused: bad signature", reason=str(exc))
            return JSONResponse({"error": "bad_signature"}, status_code=400)
        try:
            event_id, key = id_and_key(body)
        except BadEvent as exc:
            log(logger, logging.WARNING, "webhook refused: bad payload", reason=str(exc))
            return JSONResponse({"error": "bad_payload"}, status_code=400)

        headers = {
            "content-type": "application/json",
            "received-at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        try:
            partition, offset = await asyncio.to_thread(publisher.publish, topic, key, body, headers)
        except PublishError as exc:
            log(
                logger,
                logging.ERROR,
                "webhook not queued; asking the provider to retry",
                event_id=event_id,
                reason=str(exc),
            )
            return JSONResponse({"error": "queue_unavailable"}, status_code=503, headers={"Retry-After": "5"})
        log(logger, logging.INFO, "webhook queued", event_id=event_id, key=key, partition=partition, offset=offset)
        return JSONResponse({"received": True, "event_id": event_id})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        if not publisher.healthy():
            return JSONResponse({"status": "kafka unreachable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app


def main() -> None:
    configure_logging("webhook-receiver")
    publisher = KafkaPublisher(env_str("KAFKA_BOOTSTRAP", "kafka:9092"), client_id="webhook-receiver")
    app = create_app(
        publisher,
        secrets=[secret.encode() for secret in env_list("WEBHOOK_SECRETS")],
        topic=env_str("KAFKA_TOPIC", EVENTS_TOPIC),
        tolerance_seconds=env_int("SIGNATURE_TOLERANCE_SECONDS", signatures.DEFAULT_TOLERANCE_SECONDS),
    )
    uvicorn.run(app, host="0.0.0.0", port=env_int("PORT", 8000), log_config=None, access_log=False)


if __name__ == "__main__":
    main()
