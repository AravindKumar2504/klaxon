from klaxon.patient.services.idempotency import KEY_TTL_SECONDS, LOCK_TTL_SECONDS, Claim, RedisIdempotency
from tests.services.fakes import FakeRedis


def test_a_key_moves_from_new_to_running_to_replayed():
    redis = FakeRedis()
    keys = RedisIdempotency(redis)
    claim = keys.claim("payments", "k", "fp")
    assert claim.kind == "new"
    assert redis.ttls["idem:payments:k"] == LOCK_TTL_SECONDS
    assert keys.claim("payments", "k", "fp").kind == "in_progress"
    assert keys.complete("payments", "k", claim, "fp", 201, {"id": "pmt_000001"}) is True
    replay = keys.claim("payments", "k", "fp")
    assert (replay.kind, replay.status, replay.body) == ("replay", 201, {"id": "pmt_000001"})
    assert redis.ttls["idem:payments:k"] == KEY_TTL_SECONDS


def test_the_same_key_with_a_different_request_is_a_mismatch():
    keys = RedisIdempotency(FakeRedis())
    keys.claim("payments", "k", "fp-1")
    assert keys.claim("payments", "k", "fp-2").kind == "mismatch"


def test_a_released_key_can_be_claimed_again():
    keys = RedisIdempotency(FakeRedis())
    claim = keys.claim("payments", "k", "fp")
    keys.release("payments", "k", claim)
    assert keys.claim("payments", "k", "fp").kind == "new"


def test_scopes_do_not_collide():
    keys = RedisIdempotency(FakeRedis())
    keys.claim("payments", "k", "fp")
    assert keys.claim("refunds", "k", "fp").kind == "new"


def test_redis_down_is_reported_not_raised():
    redis = FakeRedis()
    redis.down = True
    keys = RedisIdempotency(redis)
    assert keys.claim("payments", "k", "fp").kind == "unavailable"
    assert keys.complete("payments", "k", Claim("unavailable"), "fp", 201, {}) is False  # does not raise
    keys.release("payments", "k", Claim("unavailable"))  # does not raise
    assert keys.healthy() is False


def test_a_request_whose_claim_expired_cannot_release_the_newer_claim():
    redis = FakeRedis()
    keys = RedisIdempotency(redis)
    slow = keys.claim("payments", "k", "fp")
    redis.expire_now("idem:payments:k")  # the slow request outlived its lock
    newer = keys.claim("payments", "k", "fp")
    assert newer.kind == "new"
    keys.release("payments", "k", slow)  # the slow request gives up
    assert keys.claim("payments", "k", "fp").kind == "in_progress"  # the newer claim is still there
    keys.release("payments", "k", newer)
    assert keys.claim("payments", "k", "fp").kind == "new"


def test_a_request_whose_claim_expired_cannot_overwrite_the_newer_claim():
    redis = FakeRedis()
    keys = RedisIdempotency(redis)
    slow = keys.claim("payments", "k", "fp")
    redis.expire_now("idem:payments:k")
    keys.claim("payments", "k", "fp")
    assert keys.complete("payments", "k", slow, "fp", 201, {"id": "pmt_000001"}) is False
    assert keys.claim("payments", "k", "fp").kind == "in_progress"


def test_a_finished_answer_is_saved_even_if_the_lock_expired_and_nobody_else_came():
    redis = FakeRedis()
    keys = RedisIdempotency(redis)
    claim = keys.claim("payments", "k", "fp")
    redis.expire_now("idem:payments:k")
    assert keys.complete("payments", "k", claim, "fp", 201, {"id": "pmt_000001"}) is True
    assert keys.claim("payments", "k", "fp").kind == "replay"


def test_each_claim_has_its_own_token():
    keys = RedisIdempotency(FakeRedis())
    first = keys.claim("payments", "a", "fp")
    second = keys.claim("payments", "b", "fp")
    assert first.held != second.held
