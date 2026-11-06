import asyncio

import httpx
from celery import Celery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.alerts.delivery import deliver_alert
from app.config import get_settings

celery = Celery("home", broker=get_settings().redis_url)


async def _run(alert_id: int) -> None:
    """Fresh engine/redis/http per asyncio.run loop: pooled connections can't cross loops."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await deliver_alert(
                alert_id,
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                redis=redis,
                http=http,
            )
    finally:
        await redis.aclose()
        await engine.dispose()


# Delivery is at-most-once by design: acks_late stays OFF (Celery acks on receipt), so a
# worker crash loses the alert rather than re-sending alarms; deliver_alert also claims the
# alert atomically, so a duplicate message is a no-op.
@celery.task(name="deliver_alert")
def deliver_alert_task(alert_id: int) -> None:
    asyncio.run(_run(alert_id))
