import time
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis

from app.redis import get_redis


def by_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(
    scope: str, limit: int, per_seconds: int, key: Callable[[Request], str]
) -> Callable[..., Awaitable[None]]:
    async def dependency(request: Request, redis: Annotated[Redis, Depends(get_redis)]) -> None:
        window = int(time.time()) // per_seconds
        name = f"rl:{scope}:{key(request)}:{window}"
        count = await redis.incr(name)
        if count == 1:
            await redis.expire(name, per_seconds)
        if count > limit:
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    return dependency
