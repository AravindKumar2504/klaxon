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
check accepts that. Refunds work the same way (week 2): our refund id goes to
the provider as metadata, and the refund's webhook confirms it.

When the provider keeps failing, the provider client's circuit breaker fails
fast (week 4, see provider_client): the call is refused at once instead of
waiting out its timeouts, and the payment or refund stays pending the same way.

A refund is refused when it, with every other refund of the payment that may
have gone through, adds up to more than the payment (week 4, known gap 1): a
succeeded refund counts, and so does a pending one, whose outcome is unknown.
So a refund stuck pending blocks a second one until it settles (a retry with
its key, or its webhook). Only a failed refund does not count.

The ledger is not written here. Money is booked only from the provider's
webhooks, one path for every payment (see ledger_consumer).

Telemetry (week 2): a payment or refund request's span carries our payment
id (once it is known to be ours) and the provider's, the provider calls are
traced and timed (httpx), and klaxon.payments.pending counts the payments
whose outcome we do not know yet, split by age: under or over a minute.
Payments pending for over a minute are the "stuck" symptom. Health checks and
the gauge's query make no spans: they run every 10 seconds.

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
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.metrics import CallbackOptions, Observation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from klaxon.patient.services import telemetry
from klaxon.patient.services.common import (
    add_request_logging,
    add_trusted_hosts,
    configure_logging,
    env_float,
    env_int,
    env_str,
    log,
)
from klaxon.patient.services.events import ID, MAX_AMOUNT_CENTS
from klaxon.patient.services.idempotency import RedisIdempotency
from klaxon.patient.services.payments_store import PaymentRow, PaymentsStore, PgPaymentsStore, RefundRow
from klaxon.patient.services.provider_client import (
    ProviderClient,
    ProviderRejected,
    ProviderUnavailable,
    breaker_setting,
)

logger = logging.getLogger("klaxon.payments_api")

REPLAYED_HEADER = "Idempotent-Replayed"
KEY_REUSED = "this Idempotency-Key was used for a different request"
OUTCOME_UNKNOWN = "the outcome is unknown; retry with the same Idempotency-Key"
STUCK_AFTER_SECONDS = 60  # a payment pending longer than this is reported as stuck
# The refunds of a payment that count against it: those that may have gone through. A pending one's outcome is
# unknown, so it counts until it settles (known gap 1, decided 2026-10-14). Only a failed one does not.
COUNTED_REFUNDS = ("pending", "succeeded")
EXCEEDS = "more than what is left to refund, counting the refunds whose outcome is not known yet"


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


def provider_id(answer: object) -> str | None:
    """The id in the provider's answer, if it is a plain id. The answer is outside input: anything else is not
    trusted, and never reaches our records, our logs or a span."""
    found = answer.get("id") if isinstance(answer, dict) else None
    return found if isinstance(found, str) and ID.fullmatch(found) else None


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
    app = FastAPI(title="Payments API", telemetry=telemetry.FASTAPI)
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
            trace.get_current_span().set_attribute("klaxon.payment_id", row.id)
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
                return refused(503, "provider_unavailable", OUTCOME_UNKNOWN, final=False, payment=payment_view(row))
            except ProviderRejected as exc:
                row = store.mark_payment(row.id, "failed", None)
                log(logger, logging.WARNING, "provider refused the payment", payment_id=row.id, error=str(exc))
                return refused(402, "payment_failed", exc.message or exc.code, final=True, payment=payment_view(row))
            charge_id = provider_id(charge)
            if charge_id is None:  # an answer we cannot trust is as good as none: the webhook will settle it
                log(
                    logger,
                    logging.ERROR,
                    "the provider's answer names no plain id; payment left pending",
                    payment_id=row.id,
                )
                return refused(503, "provider_unavailable", OUTCOME_UNKNOWN, final=False, payment=payment_view(row))
            row = store.mark_payment(row.id, "succeeded", charge_id)
            trace.get_current_span().set_attribute("provider.payment_id", charge_id)
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
            trace.get_current_span().set_attribute("klaxon.payment_id", payment.id)  # ours now, not the caller's text
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
                others = [r for r in store.refunds_for(payment_id) if r.id != row.id]
                counted = sum(r.amount_cents for r in others if r.status in COUNTED_REFUNDS)
                if counted + row.amount_cents > payment.amount_cents:
                    row = store.mark_refund(row.id, "failed", None)
                    log(
                        logger,
                        logging.WARNING,
                        "refund refused: more than what is left to refund",
                        refund_id=row.id,
                        payment_id=payment_id,
                        amount_cents=row.amount_cents,
                        counted_cents=counted,
                        pending_cents=sum(r.amount_cents for r in others if r.status == "pending"),
                    )
                    return refused(422, "refund_exceeds_payment", EXCEEDS, final=True, refund=refund_view(row))
            try:
                result = provider.create_refund(
                    payment.provider_payment_id, row.amount_cents, row.id, metadata={"klaxon_refund_id": row.id}
                )
            except ProviderUnavailable as exc:
                log(
                    logger, logging.ERROR, "provider unavailable; refund left pending", refund_id=row.id, error=str(exc)
                )
                return refused(503, "provider_unavailable", OUTCOME_UNKNOWN, final=False, refund=refund_view(row))
            except ProviderRejected as exc:
                row = store.mark_refund(row.id, "failed", None)
                log(logger, logging.WARNING, "provider refused the refund", refund_id=row.id, error=str(exc))
                return refused(422, "refund_failed", exc.message or exc.code, final=True, refund=refund_view(row))
            refund_id = provider_id(result)
            if refund_id is None:  # as for a payment: the refund's webhook will settle it
                log(
                    logger,
                    logging.ERROR,
                    "the provider's answer names no plain id; refund left pending",
                    refund_id=row.id,
                )
                return refused(503, "provider_unavailable", OUTCOME_UNKNOWN, final=False, refund=refund_view(row))
            row = store.mark_refund(row.id, "succeeded", refund_id)
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
        with suppress_instrumentation():  # no spans for the database and Redis pings, every 10 seconds
            try:
                store.ping()
            except Exception as exc:
                return JSONResponse({"status": "database unreachable", "error": str(exc)}, status_code=503)
            # Redis down is degraded, not down: the database still guards every key.
            return JSONResponse({"status": "ok" if idempotency.healthy() else "degraded: redis unreachable"})

    return app


def pending_observations(store: PaymentsStore, stuck_after_seconds: int = STUCK_AFTER_SECONDS) -> list[Observation]:
    """The pending payments, as metric values: younger than a minute, and older (stuck)."""
    try:
        with suppress_instrumentation():  # a query every 10 seconds is not worth a trace
            pending, stuck = store.pending_counts(stuck_after_seconds)
    except Exception as exc:  # the database is down: no value this time, rather than a wrong one
        log(logger, logging.WARNING, "could not count pending payments", error=str(exc))
        return []
    return [Observation(pending - stuck, {"age": "under_1m"}), Observation(stuck, {"age": "over_1m"})]


def observe_pending_payments(store: PaymentsStore) -> None:
    """Report the pending payments as a metric, each time metrics are exported."""

    def measure(options: CallbackOptions) -> list[Observation]:
        return pending_observations(store)

    metrics.get_meter("klaxon.payments").create_observable_gauge(
        "klaxon.payments.pending",
        callbacks=[measure],
        unit="{payment}",
        description="Payments whose outcome we do not know yet, by age. Old ones are stuck.",
    )


def provider_from_env() -> ProviderClient:
    """The provider client, from this service's settings: its circuit breaker on unless PROVIDER_BREAKER=off."""
    return ProviderClient(
        env_str("PROVIDER_URL", "http://fake-provider:8000"),
        timeout_seconds=env_float("PROVIDER_TIMEOUT_SECONDS", 3.0),
        breaker=breaker_setting(env_str("PROVIDER_BREAKER", "on")),
    )


def main() -> None:
    configure_logging("payments-api")
    telemetry.setup("payments-api")
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
    provider = provider_from_env()
    store = PgPaymentsStore(pool)
    observe_pending_payments(store)
    app = create_app(store, RedisIdempotency(redis_client), provider)
    uvicorn.run(app, host="0.0.0.0", port=env_int("PORT", 8000), log_config=None, access_log=False)


if __name__ == "__main__":
    main()
