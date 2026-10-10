"""Idempotency keys for our payments API: Redis is the fast path, PostgreSQL the guarantee.

A client whose request timed out cannot know whether the payment went
through, so it retries with the same Idempotency-Key header. Redis remembers
each key for 24 hours: a repeat gets the saved response instead of a second
payment, and a repeat that arrives while the first request is still running
gets 409 (try again shortly).

Each claim carries a random token. Releasing a claim, or saving the final
response, happens in a small Lua script that first checks the key still holds
that token, so a slow request whose claim has expired can never delete or
overwrite the claim of a newer request with the same key.

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
import uuid
from dataclasses import dataclass

import redis

from klaxon.patient.services.common import log

logger = logging.getLogger("klaxon.idempotency")

KEY_TTL_SECONDS = 24 * 3600  # how long a finished response is replayed (Stripe also keeps keys for 24 hours)
# How long a running request holds its key. Longer than the slowest request the API allows: up to 5 s
# waiting for a database connection, plus three provider attempts of 3 s each with backoff (about 15 s).
LOCK_TTL_SECONDS = 60

KINDS = ("new", "replay", "in_progress", "mismatch", "unavailable")

# Delete the key only if it still holds our claim.
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# Save the final response if the key still holds our claim, or if nobody holds it any more.
COMPLETE = """
local current = redis.call('GET', KEYS[1])
if current == false or current == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
end
return 0
"""


@dataclass(frozen=True)
class Claim:
    kind: str  # one of KINDS
    status: int | None = None  # for "replay": the saved response
    body: dict | None = None
    held: str | None = None  # for "new": the exact value we stored, with our token in it


class RedisIdempotency:
    def __init__(self, client: redis.Redis, prefix: str = "idem") -> None:
        self.client = client
        self.prefix = prefix

    def _name(self, scope: str, key: str) -> str:
        return f"{self.prefix}:{scope}:{key}"

    def claim(self, scope: str, key: str, fingerprint: str) -> Claim:
        """Try to take the key for this request. "new" means it is ours; release or complete it later."""
        name = self._name(scope, key)
        running = json.dumps({"state": "in_progress", "fingerprint": fingerprint, "token": uuid.uuid4().hex})
        try:
            for _ in range(2):  # a second pass covers a key that expired between SET and GET
                if self.client.set(name, running, nx=True, ex=LOCK_TTL_SECONDS):
                    return Claim("new", held=running)
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

    def complete(self, scope: str, key: str, claim: Claim, fingerprint: str, status: int, body: dict) -> bool:
        """Save the final response, so repeats of this key replay it. False if a newer request holds the key."""
        record = json.dumps({"state": "done", "fingerprint": fingerprint, "status": status, "body": body})
        try:
            saved = self.client.eval(COMPLETE, 1, self._name(scope, key), claim.held or "", record, KEY_TTL_SECONDS)
        except redis.RedisError as exc:
            log(logger, logging.WARNING, "could not save the response in redis", error=str(exc))
            return False
        return bool(saved)

    def release(self, scope: str, key: str, claim: Claim) -> None:
        """Give our claim back without a saved response (the request did not finish), so a retry can run."""
        if claim.held is None:
            return  # we never held the key (Redis was down), so there is nothing of ours to give back
        try:
            self.client.eval(RELEASE, 1, self._name(scope, key), claim.held)
        except redis.RedisError as exc:
            log(logger, logging.WARNING, "could not release the key in redis; it expires on its own", error=str(exc))

    def healthy(self) -> bool:
        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False
