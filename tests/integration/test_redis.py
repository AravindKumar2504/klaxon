"""RedisIdempotency's Lua scripts against the stack's real Redis (the offline tests use a Python stand-in)."""

import uuid

import pytest
import redis

from klaxon.patient.services.idempotency import LOCK_TTL_SECONDS, RedisIdempotency

pytestmark = pytest.mark.integration


@pytest.fixture
def keys():
    client = redis.Redis(host="127.0.0.1", port=6379, socket_timeout=2)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.fail(f"the stack's Redis is not reachable (make up): {exc}")
    prefix = f"test-{uuid.uuid4().hex[:8]}"  # never touches the API's own keys
    yield client, RedisIdempotency(client, prefix=prefix), prefix
    for name in client.scan_iter(f"{prefix}:*"):
        client.delete(name)
    client.close()


def test_claim_running_complete_replay(keys):
    client, idem, prefix = keys
    claim = idem.claim("payments", "k", "fp")
    assert claim.kind == "new"
    assert 0 < client.ttl(f"{prefix}:payments:k") <= LOCK_TTL_SECONDS
    assert idem.claim("payments", "k", "fp").kind == "in_progress"
    assert idem.complete("payments", "k", claim, "fp", 201, {"id": "pmt_000001"}) is True
    replay = idem.claim("payments", "k", "fp")
    assert (replay.kind, replay.status, replay.body) == ("replay", 201, {"id": "pmt_000001"})


def test_an_expired_claim_cannot_release_or_overwrite_a_newer_one(keys):
    client, idem, prefix = keys
    slow = idem.claim("payments", "k", "fp")
    client.delete(f"{prefix}:payments:k")  # what the lock's expiry does
    newer = idem.claim("payments", "k", "fp")
    idem.release("payments", "k", slow)
    assert idem.complete("payments", "k", slow, "fp", 201, {}) is False
    assert idem.claim("payments", "k", "fp").kind == "in_progress"
    idem.release("payments", "k", newer)
    assert idem.claim("payments", "k", "fp").kind == "new"
