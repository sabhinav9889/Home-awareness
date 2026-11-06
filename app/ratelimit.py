import time
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis

from app.redis import get_redis
from app.security import InvalidToken, decode_access_token, hash_token


def by_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def by_device_key(request: Request) -> str:
    return hash_token(request.headers.get("X-Device-Key", ""))


def by_member(request: Request) -> str:
    """Key on the member id inside the bearer JWT: no DB lookup, and a forged or invalid
    token (which the auth dependency rejects with 401 anyway) falls back to its own hash."""
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip()
    try:
        return f"m{decode_access_token(token).member_id}"
    except InvalidToken:
        return hash_token(header)


def rate_limit(
    scope: str, limit: int, per_seconds: int, key: Callable[[Request], str]
) -> Callable[..., Awaitable[None]]:
    async def dependency(request: Request, redis: Annotated[Redis, Depends(get_redis)]) -> None:
        window = int(time.time()) // per_seconds
        name = f"rl:{scope}:{key(request)}:{window}"
        # One round trip; the key is per-window, so refreshing its TTL every call is harmless.
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(name)
            pipe.expire(name, per_seconds)
            count, _ = await pipe.execute()
        if count > limit:
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    return dependency
