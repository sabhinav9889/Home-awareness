"""Offline sensor detection: a silent sensor is worse than none, so warn when one stops."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import ntfy, telegram
from app.auth.models import Member
from app.config import get_settings
from app.devices.models import Device
from app.live.hub import publish_to_household
from app.metrics import DEVICES_OFFLINE
from app.rules.models import AlertPreference
from app.tenancy import Tenant

log = logging.getLogger(__name__)

OFFLINE_AFTER = timedelta(seconds=180)
CHECK_INTERVAL_S = 60


def offline_text(name: str) -> str:
    return f"📴 Sensor {name} is offline — alerts from it have stopped."


async def _notify_members(
    session: AsyncSession, household_id: int, text: str, http: httpx.AsyncClient, settings: Any
) -> None:
    tenant = Tenant.for_household(household_id, session)
    members = (await session.execute(tenant.select(Member))).scalars().all()
    prefs = {
        p.member_id: p for p in (await session.execute(tenant.select(AlertPreference))).scalars()
    }
    token = settings.telegram_bot_token
    for member in members:
        p = prefs.get(member.id)  # missing row = defaults (nothing external enabled)
        sends = []
        if p and p.telegram and p.telegram_chat_id and token:
            sends.append(("telegram", telegram.send_message(http, token, p.telegram_chat_id, text)))
        if p and p.ntfy_topic:
            sends.append(("ntfy", ntfy.publish(http, settings.ntfy_base_url, p.ntfy_topic, text)))
        for channel, coro in sends:
            try:
                await coro  # one attempt each; the offline warning is best effort
            except Exception as e:  # noqa: BLE001
                error = str(e) or type(e).__name__
                if token:
                    error = error.replace(token, "***")
                log.warning(
                    "offline warning via %s to member %s failed: %s", channel, member.id, error
                )


async def check_offline_devices(
    session: AsyncSession,
    redis: Redis,
    now: datetime,
    *,
    http: httpx.AsyncClient | None = None,
    settings: Any = None,
) -> list[int]:
    """Mark stale online devices offline (atomically, once) and warn their households."""
    if http is None:
        async with httpx.AsyncClient(timeout=10) as own_http:
            return await check_offline_devices(
                session, redis, now, http=own_http, settings=settings
            )
    settings = settings or get_settings()
    # Cross-household sweep, so it cannot go through a single-household Tenant. The atomic
    # claim means overlapping runs never both notify for the same device.
    rows = (
        await session.execute(
            update(Device)
            .where(Device.status == "online", Device.last_seen_at < now - OFFLINE_AFTER)
            .values(status="offline")
            .returning(Device.id, Device.household_id, Device.name)
        )
    ).all()
    await session.commit()
    if rows:
        DEVICES_OFFLINE.inc(len(rows))
    for device_id, household_id, name in rows:
        try:
            await publish_to_household(
                redis,
                household_id,
                {"kind": "device_offline", "device_id": device_id, "name": name},
            )
        except Exception:  # noqa: BLE001  never let a live-publish failure skip the warnings
            log.warning("live offline publish for device %s failed", device_id, exc_info=True)
        try:
            await _notify_members(session, household_id, offline_text(name), http, settings)
        except Exception:  # noqa: BLE001  one household's failure must not block the others
            log.warning("offline notification for device %s failed", device_id, exc_info=True)
    return [r[0] for r in rows]


async def offline_loop(
    session_factory: Callable[[], Any],
    redis: Redis,
    *,
    interval_s: float = CHECK_INTERVAL_S,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """In-process scheduler. Logs and continues on any error; stops only when cancelled."""
    clock = clock or (lambda: datetime.now(UTC))
    async with httpx.AsyncClient(timeout=10) as http:
        while True:
            try:
                async with session_factory() as session:
                    await check_offline_devices(session, redis, clock(), http=http)
            except Exception:  # noqa: BLE001
                log.exception("offline check failed; will retry")
            await asyncio.sleep(interval_s)
