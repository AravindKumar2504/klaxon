"""Our client for the fake provider's API: timeouts, and retries that are safe to make.

Every call carries an idempotency key, so a retry after a timeout cannot charge
twice: the provider answers a repeated key with the first result. Errors the
provider will not change its mind about (4xx other than 409 and 429) are not
retried.

There is no circuit breaker yet: when the provider hangs, every request waits
for its timeouts. That gap is deliberate for now; a provider outage is one of
the planned incidents (week 2).

Telemetry: klaxon.provider.attempts counts every attempt by its result, an
HTTP status or "no_answer" (with the error's type). The httpx instrumentation
records a call's duration only when an answer came back, so without this
count a provider that times out would not show in the metrics at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from opentelemetry import metrics

RETRYABLE_STATUSES = {409, 429}  # "a request with this key is still running" and "slow down"
ATTEMPTS = metrics.get_meter("klaxon.provider_client").create_counter(
    "klaxon.provider.attempts",
    unit="{attempt}",
    description="Every call to the provider, retries included, by result: an HTTP status, or no_answer.",
)


class ProviderUnavailable(Exception):
    """No answer we can trust (timeouts, connection errors, 5xx), even after retries. The outcome is unknown."""


class ProviderRejected(Exception):
    """The provider answered and refused (4xx). The outcome is known: nothing was charged."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class ProviderClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 3.0,
        attempts: int = 3,
        backoff_seconds: float = 0.2,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = http or httpx.Client(base_url=base_url, timeout=timeout_seconds)
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep

    def create_payment(
        self, customer: str, amount_cents: int, currency: str, idempotency_key: str, metadata: dict[str, str]
    ) -> dict:
        body = {"customer": customer, "amount_cents": amount_cents, "currency": currency, "metadata": metadata}
        return self._post("/v1/payments", body, idempotency_key)

    def create_refund(self, payment_id: str, amount_cents: int, idempotency_key: str) -> dict:
        return self._post("/v1/refunds", {"payment_id": payment_id, "amount_cents": amount_cents}, idempotency_key)

    def _post(self, path: str, body: dict, idempotency_key: str) -> dict:
        problem = "no attempt made"
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.http.post(path, json=body, headers={"Idempotency-Key": idempotency_key})
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__}: {exc}"
                ATTEMPTS.add(1, {"path": path, "result": "no_answer", "error": type(exc).__name__})
            else:
                ATTEMPTS.add(1, {"path": path, "result": str(response.status_code)})
                if response.status_code < 300:
                    return response.json()
                if response.status_code < 500 and response.status_code not in RETRYABLE_STATUSES:
                    error = _error_of(response)
                    raise ProviderRejected(
                        response.status_code, error.get("code", "rejected"), error.get("message", "")
                    )
                problem = f"HTTP {response.status_code}"
            if attempt < self.attempts:
                self.sleep(self.backoff_seconds * 2 ** (attempt - 1))
        raise ProviderUnavailable(f"{path} failed after {self.attempts} attempts: {problem}")


def _error_of(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}
