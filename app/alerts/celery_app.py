import asyncio
from datetime import UTC, datetime

import httpx
from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.alerts.delivery import deliver_alert
from app.config import get_settings
from app.logging import protect_logs

celery = Celery("home", broker=get_settings().redis_url)
# Imported once in the worker's parent before it forks, so every child shares this value:
# the offline check's startup grace (see check_offline_devices) counts from worker start.
PROCESS_STARTED_AT = datetime.now(UTC)

protect_logs()  # at import, and again after Celery (re)configures logging in worker/beat
after_setup_logger.connect(lambda **_: protect_logs(), weak=False)
after_setup_task_logger.connect(lambda **_: protect_logs(), weak=False)


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


async def _check_offline() -> None:
    from app.devices import offline

    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await offline.check_offline_devices(
                session, redis, datetime.now(UTC), process_started_at=PROCESS_STARTED_AT
            )
    except Exception:
        await offline.open_quiet_window(redis, datetime.now(UTC))  # then let Celery log it
        raise
    finally:
        await redis.aclose()
        await engine.dispose()


@celery.task(name="check_offline_devices")
def check_offline_devices_task() -> None:
    asyncio.run(_check_offline())


async def _sweep_stale() -> None:
    from app.alerts.sweep import sweep_snapshots, sweep_stale_alerts

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await sweep_stale_alerts(session, datetime.now(UTC))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await sweep_snapshots(session, datetime.now(UTC))
    finally:
        await engine.dispose()


@celery.task(name="sweep_stale_alerts")
def sweep_stale_alerts_task() -> None:
    asyncio.run(_sweep_stale())


celery.conf.beat_schedule = {
    "check-offline-devices": {"task": "check_offline_devices", "schedule": 60.0},
    "sweep-stale-alerts": {"task": "sweep_stale_alerts", "schedule": 300.0},
}
