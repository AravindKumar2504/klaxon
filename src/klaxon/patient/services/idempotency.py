"""Idempotency keys for our payments API: Redis is the fast path, PostgreSQL the guarantee.

A client whose request timed out cannot know whether the payment went
through, so it retries with the same Idempotency-Key header. Redis remembers
each key for 24 hours: a repeat gets the saved response instead of a second
payment, and a repeat that arrives while the first request is still running
gets 409 (try again shortly).

Redis is not the guarantee. Keys expire, this stack runs Redis without
persistence (a restart forgets every key), and Redis can be down. Behind it,
payments.idempotency_key is UNIQUE in PostgreSQL, so a repeated key always
finds the existing payment and never creates a second one. When Redis is
down the API logs it and carries on with the database alone: it fails open to
the database, never open to nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import redis

from klaxon.patient.services.common import log

logger = logging.getLogger("klaxon.idempotency")

KEY_TTL_SECONDS = 24 * 3600  # how long a finished response is replayed (Stripe also keeps keys for 24 hours)
LOCK_TTL_SECONDS = 30  # how long a running request holds its key if the API dies mid-request

KINDS = ("new", "replay", "in_progress", "mismatch", "unavailable")


@dataclass(frozen=True)
class Claim:
    kind: str  # one of KINDS
    status: int | None = None  # for "replay": the saved response
    body: dict | None = None


class RedisIdempotency:
    def __init__(self, client: redis.Redis, prefix: str = "idem") -> None:
        self.client = client
        self.prefix = prefix

    def _name(self, scope: str, key: str) -> str:
        return f"{self.prefix}:{scope}:{key}"

    def claim(self, scope: str, key: str, fingerprint: str) -> Claim:
        """Try to take the key for this request. "new" means it is ours; release or complete it later."""
        name = self._name(scope, key)
        running = json.dumps({"state": "in_progress", "fingerprint": fingerprint})
        try:
            for _ in range(2):  # a second pass covers a key that expired between SET and GET
                if self.client.set(name, running, nx=True, ex=LOCK_TTL_SECONDS):
                    return Claim("new")
                saved = self.client.get(name)
                if saved is not None:
                    break
            else:
                return Claim("in_progress")
        except redis.RedisError as exc:
            log(logger, logging.WARNING, "redis unavailable; the database alone guards this key", error=str(exc))
            return Claim("unavailable")
        record = json.loads(saved)
        if record["fingerprint"] != fingerprint:
            return Claim("mismatch")
        if record["state"] == "in_progress":
            return Claim("in_progress")
        return Claim("replay", record["status"], record["body"])

    def complete(self, scope: str, key: str, fingerprint: str, status: int, body: dict) -> None:
        """Save the final response, so repeats of this key replay it."""
        record = {"state": "done", "fingerprint": fingerprint, "status": status, "body": body}
        try:
            self.client.set(self._name(scope, key), json.dumps(record), ex=KEY_TTL_SECONDS)
        except redis.RedisError as exc:
            log(logger, logging.WARNING, "could not save the response in redis", error=str(exc))

    def release(self, scope: str, key: str) -> None:
        """Give the key back without a saved response (the request did not finish), so a retry can run."""
        try:
            self.client.delete(self._name(scope, key))
        except redis.RedisError as exc:
            log(logger, logging.WARNING, "could not release the key in redis; it expires on its own", error=str(exc))

    def healthy(self) -> bool:
        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False
