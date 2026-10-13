"""The fake provider's HTTP API: a simulator of a Stripe-like provider (synthetic data only).

    POST /v1/payments        create and capture a payment (Idempotency-Key honored)
    POST /v1/refunds         refund part or all of a payment (Idempotency-Key honored)
    GET  /v1/payments        every payment, oldest first, paginated (limit, starting_after)
    GET  /v1/payments/{id}
    GET  /v1/events          every event, oldest first, paginated (limit, starting_after)
    GET  /v1/balance         what the provider holds for us: captured minus refunded

Simulator controls, not part of any real provider:

    GET  /admin/deliveries   delivery counts; ?attempts=true lists every attempt
    PUT  /admin/behavior     lose acknowledgements on purpose, to cause duplicates; answer a
                             charge with a 500, to leave our side not knowing it went through
    GET  /healthz

Run: python -m klaxon.patient.services.provider_app
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from klaxon.patient.services.common import (
    add_request_logging,
    add_trusted_hosts,
    configure_logging,
    env_float,
    env_int,
    env_str,
    log,
)
from klaxon.patient.services.provider_delivery import Behavior, WebhookSender
from klaxon.patient.services.provider_store import ProviderError, ProviderStore

logger = logging.getLogger("klaxon.provider")

REPLAYED_HEADER = "Idempotent-Replayed"  # Stripe's name for "this is the saved response to a repeated key"


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer: str = Field(min_length=1, max_length=64)
    amount_cents: StrictInt = Field(gt=0)
    currency: Literal["usd"] = "usd"
    metadata: dict[str, str] = Field(default_factory=dict, max_length=20)


class RefundIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment_id: str = Field(min_length=1, max_length=64)
    amount_cents: StrictInt = Field(gt=0)


class BehaviorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lose_ack_rate: float = Field(0.0, ge=0.0, le=1.0)
    seed: int = 0
    lose_ack_event_ids: list[str] = Field(default_factory=list)
    fail_after_charge_rate: float = Field(0.0, ge=0.0, le=1.0)
    fail_after_charge_keys: list[str] = Field(default_factory=list)


def _respond(body: dict, replayed: bool) -> JSONResponse:
    return JSONResponse(body, headers={REPLAYED_HEADER: "true"} if replayed else None)


def create_app(store: ProviderStore, sender: WebhookSender | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if sender is not None:
            sender.start()
        yield
        if sender is not None:
            sender.stop()

    # No telemetry from the provider: it stands for an outside company (see telemetry). Its process sets up
    # no providers, so FastAPI's own telemetry stays off; this keeps FastAPI from setting any up itself.
    app = FastAPI(title="Fake payment provider (simulator)", lifespan=lifespan, telemetry={"auto_configure": False})
    add_request_logging(app, logger)
    add_trusted_hosts(app, "fake-provider")

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.post("/v1/payments")
    def create_payment(
        payment: PaymentIn, idempotency_key: str | None = Header(None, min_length=1, max_length=255)
    ) -> JSONResponse:
        body, replayed = store.create_payment(
            payment.customer, payment.amount_cents, payment.currency, payment.metadata, idempotency_key
        )
        if sender is not None and not replayed:
            sender.wake()
        if sender is not None and sender.behavior.fails_after_charge(idempotency_key or body["id"]):
            # Simulated: the charge is saved and its webhook is on its way, but the answer is an error.
            log(logger, logging.WARNING, "charged, then answered 500 on purpose", payment_id=body["id"])
            return JSONResponse(
                {"error": {"code": "internal_error", "message": "simulated failure after the charge was saved"}},
                status_code=500,
            )
        return _respond(body, replayed)

    @app.post("/v1/refunds")
    def create_refund(
        refund: RefundIn, idempotency_key: str | None = Header(None, min_length=1, max_length=255)
    ) -> JSONResponse:
        body, replayed = store.create_refund(refund.payment_id, refund.amount_cents, idempotency_key)
        if sender is not None and not replayed:
            sender.wake()
        return _respond(body, replayed)

    @app.get("/v1/payments")
    def list_payments(limit: int = Query(100, ge=1, le=500), starting_after: str | None = None) -> dict:
        payments, has_more = store.list_payments(limit, starting_after)
        return {"object": "list", "data": payments, "has_more": has_more}

    @app.get("/v1/payments/{payment_id}")
    def get_payment(payment_id: str) -> dict:
        return store.get_payment(payment_id)

    @app.get("/v1/events")
    def list_events(limit: int = Query(100, ge=1, le=500), starting_after: str | None = None) -> dict:
        events, has_more = store.list_events(limit, starting_after)
        return {"object": "list", "data": events, "has_more": has_more}

    @app.get("/v1/balance")
    def balance() -> dict:
        return {"object": "balance", "available_cents": store.balance_cents(), "currency": "usd"}

    @app.get("/admin/deliveries")
    def deliveries(attempts: bool = False) -> dict:
        out = {"stats": store.delivery_stats()}
        if attempts:
            out["attempts"] = store.delivery_attempts()
        return out

    @app.put("/admin/behavior")
    def set_behavior(behavior: BehaviorIn) -> dict:
        if sender is None:
            raise ProviderError(409, "no_sender", "this provider has no webhook sender running")
        sender.behavior = Behavior(
            lose_ack_rate=behavior.lose_ack_rate,
            seed=behavior.seed,
            lose_ack_event_ids=frozenset(behavior.lose_ack_event_ids),
            fail_after_charge_rate=behavior.fail_after_charge_rate,
            fail_after_charge_keys=frozenset(behavior.fail_after_charge_keys),
        )
        logger.warning("delivery behavior changed", extra={"fields": sender.behavior.to_dict()})
        return sender.behavior.to_dict()

    @app.get("/admin/behavior")
    def get_behavior() -> dict:
        return sender.behavior.to_dict() if sender is not None else {}

    @app.get("/healthz")
    def healthz() -> dict:
        store.balance_cents()  # the store answers
        return {"status": "ok"}

    return app


def main() -> None:
    configure_logging("fake-provider")
    store = ProviderStore(env_str("PROVIDER_DB_PATH", "/data/provider.sqlite3"))
    sender = WebhookSender(
        store,
        url=env_str("WEBHOOK_URL"),
        secret=env_str("WEBHOOK_SECRET").encode(),
        client=httpx.Client(timeout=env_float("WEBHOOK_TIMEOUT_SECONDS", 5.0)),
        behavior=Behavior(lose_ack_rate=env_float("PROVIDER_LOSE_ACK_RATE", 0.0), seed=env_int("PROVIDER_SEED", 0)),
    )
    uvicorn.run(
        create_app(store, sender), host="0.0.0.0", port=env_int("PORT", 8000), log_config=None, access_log=False
    )


if __name__ == "__main__":
    main()
