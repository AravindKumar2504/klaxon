"""Our payments API: take a payment, charge it at the provider, never twice.

    POST /payments                body {customer, amount_cents, currency}; Idempotency-Key header required
    GET  /payments/{id}
    POST /payments/{id}/refunds   body {amount_cents}; Idempotency-Key header required
    GET  /healthz

How a retried request is kept from charging twice, layer by layer:
1. Redis: the Idempotency-Key is claimed with SET NX. A repeat gets the saved
   response; a repeat that arrives while the first is still running gets 409.
2. PostgreSQL: payments.idempotency_key is UNIQUE, so even without Redis a
   repeat finds the existing payment instead of creating a new one.
3. The provider: we send our payment id as the provider's Idempotency-Key, so
   even a repeated charge request returns the first charge.

When the provider's answer never comes (timeouts, 5xx), the outcome is
unknown: the payment stays pending and the client is told to retry with the
same key. If the provider did charge, its webhook settles it: the payment
confirmer marks it succeeded (see payment_confirmer), so a retry finds it
confirmed, and it does not stay pending when the client never retries. If the
provider never charged, it stays pending: no money moved, and the money
check accepts that.

The ledger is not written here. Money is booked only from the provider's
webhooks, one path for every payment (see ledger_consumer).

Run: python -m klaxon.patient.services.payments_api
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import redis
import uvicorn
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
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
from klaxon.patient.services.events import MAX_AMOUNT_CENTS
from klaxon.patient.services.idempotency import RedisIdempotency
from klaxon.patient.services.payments_store import PaymentRow, PaymentsStore, PgPaymentsStore, RefundRow
from klaxon.patient.services.provider_client import ProviderClient, ProviderRejected, ProviderUnavailable

logger = logging.getLogger("klaxon.payments_api")

REPLAYED_HEADER = "Idempotent-Replayed"
KEY_REUSED = "this Idempotency-Key was used for a different request"


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer: str = Field(min_length=1, max_length=64)
    amount_cents: StrictInt = Field(gt=0, le=MAX_AMOUNT_CENTS)
    currency: Literal["usd"] = "usd"


class RefundIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: StrictInt = Field(gt=0, le=MAX_AMOUNT_CENTS)


@dataclass(frozen=True)
class Outcome:
    """What one request produced. final: repeats of the same key get exactly this answer."""

    status: int
    body: dict
    final: bool
    replayed: bool = False


def refused(status: int, code: str, message: str, final: bool, replayed: bool = False, **extra: object) -> Outcome:
    return Outcome(status, {"error": {"code": code, "message": message}, **extra}, final, replayed)


def fingerprint(endpoint: str, body: BaseModel) -> str:
    """What the request was, so the same key with a different request can be refused."""
    canonical = json.dumps({"endpoint": endpoint, "body": body.model_dump()}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def payment_view(payment: PaymentRow, refunds: list[RefundRow] | None = None) -> dict:
    view = {
        "id": payment.id,
        "status": payment.status,
        "customer": payment.customer,
        "amount_cents": payment.amount_cents,
        "currency": payment.currency,
        "provider_payment_id": payment.provider_payment_id,
        "created_at": payment.created_at.isoformat(),
    }
    if refunds is not None:
        view["refunds"] = [refund_view(r) for r in refunds]
    return view


def refund_view(refund: RefundRow) -> dict:
    return {
        "id": refund.id,
        "payment_id": refund.payment_id,
        "status": refund.status,
        "amount_cents": refund.amount_cents,
        "provider_refund_id": refund.provider_refund_id,
        "created_at": refund.created_at.isoformat(),
    }


def _respond(outcome: Outcome, headers: dict[str, str] | None = None) -> JSONResponse:
    headers = dict(headers or {})
    if outcome.replayed:
        headers[REPLAYED_HEADER] = "true"
    return JSONResponse(outcome.body, status_code=outcome.status, headers=headers or None)


def create_app(store: PaymentsStore, idempotency: RedisIdempotency, provider: ProviderClient) -> FastAPI:
    app = FastAPI(title="Payments API")
    add_request_logging(app, logger)
    add_trusted_hosts(app, "payments-api")

    def run_once(scope: str, key: str | None, request_fingerprint: str, work: Callable[[], Outcome]) -> JSONResponse:
        """Layer 1: the Redis claim around the work. The work itself goes through layers 2 and 3."""
        if key is None or not 1 <= len(key) <= 255:
            return _respond(refused(400, "idempotency_key_required", "send an Idempotency-Key header", final=False))
        claim = idempotency.claim(scope, key, request_fingerprint)
        if claim.kind == "replay":
            return _respond(Outcome(claim.status, claim.body, final=True, replayed=True))
        if claim.kind == "mismatch":
            return _respond(refused(422, "idempotency_key_reused", KEY_REUSED, final=False))
        if claim.kind == "in_progress":
            message = "a request with this Idempotency-Key is still running"
            return _respond(refused(409, "request_in_progress", message, final=False), {"Retry-After": "1"})

        # claim.kind is "new" (ours) or "unavailable" (Redis is down: the database alone guards the key).
        try:
            outcome = work()
        except BaseException:
            idempotency.release(scope, key, claim)
            raise
        if outcome.final:
            idempotency.complete(scope, key, claim, request_fingerprint, outcome.status, outcome.body)
        else:
            idempotency.release(scope, key, claim)  # not finished: let a retry run
        return _respond(outcome)

    @app.post("/payments", status_code=201)
    def create_payment(payment: PaymentIn, idempotency_key: str | None = Header(None)) -> JSONResponse:
        request_fingerprint = fingerprint("POST /payments", payment)

        def work() -> Outcome:
            # Layer 2: the UNIQUE idempotency key in PostgreSQL.
            row, created = store.insert_or_get_payment(
                idempotency_key, request_fingerprint, payment.customer, payment.amount_cents, payment.currency
            )
            if row.request_fingerprint != request_fingerprint:
                return refused(422, "idempotency_key_reused", KEY_REUSED, final=False)
            if not created and row.status == "succeeded":
                return Outcome(201, payment_view(row), final=True, replayed=True)
            if not created and row.status == "failed":
                return refused(
                    402, "payment_failed", "the provider refused this payment", True, True, payment=payment_view(row)
                )
            # Layer 3: new, or pending from an earlier try that did not finish. Same provider key either way.
            try:
                charge = provider.create_payment(
                    row.customer, row.amount_cents, row.currency, row.id, metadata={"klaxon_payment_id": row.id}
                )
            except ProviderUnavailable as exc:
                log(
                    logger,
                    logging.ERROR,
                    "provider unavailable; payment left pending",
                    payment_id=row.id,
                    error=str(exc),
                )
                message = "the outcome is unknown; retry with the same Idempotency-Key"
                return refused(503, "provider_unavailable", message, final=False, payment=payment_view(row))
            except ProviderRejected as exc:
                row = store.mark_payment(row.id, "failed", None)
                log(logger, logging.WARNING, "provider refused the payment", payment_id=row.id, error=str(exc))
                return refused(402, "payment_failed", exc.message or exc.code, final=True, payment=payment_view(row))
            row = store.mark_payment(row.id, "succeeded", charge["id"])
            log(
                logger,
                logging.INFO,
                "payment succeeded",
                payment_id=row.id,
                provider_payment_id=row.provider_payment_id,
                amount_cents=row.amount_cents,
            )
            return Outcome(201, payment_view(row), final=True)

        return run_once("payments", idempotency_key, request_fingerprint, work)

    @app.get("/payments/{payment_id}")
    def get_payment(payment_id: str) -> JSONResponse:
        row = store.get_payment(payment_id)
        if row is None:
            return _respond(refused(404, "no_such_payment", f"no payment {payment_id}", final=False))
        return JSONResponse(payment_view(row, store.refunds_for(payment_id)))

    @app.post("/payments/{payment_id}/refunds", status_code=201)
    def create_refund(payment_id: str, refund: RefundIn, idempotency_key: str | None = Header(None)) -> JSONResponse:
        request_fingerprint = fingerprint(f"POST /payments/{payment_id}/refunds", refund)

        def work() -> Outcome:
            payment = store.get_payment(payment_id)
            if payment is None:
                return refused(404, "no_such_payment", f"no payment {payment_id}", final=False)
            if payment.status != "succeeded" or payment.provider_payment_id is None:
                return refused(409, "payment_not_settled", "only a succeeded payment can be refunded", final=False)
            row, created = store.insert_or_get_refund(
                idempotency_key, request_fingerprint, payment_id, refund.amount_cents
            )
            if row.request_fingerprint != request_fingerprint:
                return refused(422, "idempotency_key_reused", KEY_REUSED, final=False)
            if not created and row.status == "succeeded":
                return Outcome(201, refund_view(row), final=True, replayed=True)
            if not created and row.status == "failed":
                return refused(422, "refund_failed", "this refund was refused", True, True, refund=refund_view(row))
            if created:
                refunded = sum(r.amount_cents for r in store.refunds_for(payment_id) if r.status == "succeeded")
                if refunded + row.amount_cents > payment.amount_cents:
                    row = store.mark_refund(row.id, "failed", None)
                    message = "more than what is left to refund"
                    return refused(422, "refund_exceeds_payment", message, final=True, refund=refund_view(row))
            try:
                result = provider.create_refund(payment.provider_payment_id, row.amount_cents, row.id)
            except ProviderUnavailable as exc:
                log(
                    logger, logging.ERROR, "provider unavailable; refund left pending", refund_id=row.id, error=str(exc)
                )
                message = "the outcome is unknown; retry with the same Idempotency-Key"
                return refused(503, "provider_unavailable", message, final=False, refund=refund_view(row))
            except ProviderRejected as exc:
                row = store.mark_refund(row.id, "failed", None)
                log(logger, logging.WARNING, "provider refused the refund", refund_id=row.id, error=str(exc))
                return refused(422, "refund_failed", exc.message or exc.code, final=True, refund=refund_view(row))
            row = store.mark_refund(row.id, "succeeded", result["id"])
            log(
                logger,
                logging.INFO,
                "refund succeeded",
                refund_id=row.id,
                payment_id=payment_id,
                amount=row.amount_cents,
            )
            return Outcome(201, refund_view(row), final=True)

        return run_once("refunds", idempotency_key, request_fingerprint, work)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        try:
            store.ping()
        except Exception as exc:
            return JSONResponse({"status": "database unreachable", "error": str(exc)}, status_code=503)
        # Redis down is degraded, not down: the database still guards every key.
        return JSONResponse({"status": "ok" if idempotency.healthy() else "degraded: redis unreachable"})

    return app


def main() -> None:
    configure_logging("payments-api")
    pool = ConnectionPool(
        env_str("DATABASE_URL"),
        min_size=1,
        max_size=env_int("DB_POOL_SIZE", 10),
        kwargs={"autocommit": True, "row_factory": dict_row},
        name="payments",
        timeout=env_float("DB_POOL_TIMEOUT_SECONDS", 5.0),  # fail fast instead of queueing for 30 s
        check=ConnectionPool.check_connection,  # after a PostgreSQL restart, drop dead connections first
        open=True,
    )
    redis_client = redis.Redis.from_url(
        env_str("REDIS_URL", "redis://redis:6379/0"), socket_timeout=0.5, socket_connect_timeout=0.5
    )
    provider = ProviderClient(
        env_str("PROVIDER_URL", "http://fake-provider:8000"),
        timeout_seconds=env_float("PROVIDER_TIMEOUT_SECONDS", 3.0),
    )
    app = create_app(PgPaymentsStore(pool), RedisIdempotency(redis_client), provider)
    uvicorn.run(app, host="0.0.0.0", port=env_int("PORT", 8000), log_config=None, access_log=False)


if __name__ == "__main__":
    main()
