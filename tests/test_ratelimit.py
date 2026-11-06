import time

from app import ratelimit


def test_frozen_ratelimit_clock_freezes_only_the_limiter(frozen_ratelimit_clock):
    assert ratelimit.now() == frozen_ratelimit_clock
    assert time.time() != frozen_ratelimit_clock  # global time is untouched


async def test_429_carries_retry_after_until_window_reset(client, frozen_ratelimit_clock):
    for _ in range(10):
        await client.post("/device/pair", json={"code": "AAAAAAAA"})
    r = await client.post("/device/pair", json={"code": "AAAAAAAA"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == str(60 - int(frozen_ratelimit_clock) % 60)  # "45"


async def test_limiter_pipeline_is_not_a_transaction(client, redis, monkeypatch):
    seen = []
    real = redis.pipeline

    def spy(*args, **kwargs):
        seen.append(kwargs.get("transaction", args[0] if args else True))
        return real(*args, **kwargs)

    monkeypatch.setattr(redis, "pipeline", spy)
    await client.post("/device/pair", json={"code": "AAAAAAAA"})
    assert seen == [False]  # no MULTI/EXEC around INCR+EXPIRE: half the Redis commands
