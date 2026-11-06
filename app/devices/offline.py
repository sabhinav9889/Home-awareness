"""Offline sensor detection: a silent sensor is worse than none, so warn when one stops."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select, update
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
# Fleet-silence guard upper bound: see check_offline_devices.
FLEET_SILENCE_MAX = timedelta(minutes=10)
CHECK_INTERVAL_S = 60
# Epoch seconds before which no device may be marked offline (see check_offline_devices).
QUIET_KEY = "offline:quiet_until"


async def open_quiet_window(redis: Redis, now: datetime) -> None:
    """After a fleet-wide silence or a failed check, give every sensor a full OFFLINE_AFTER
    window to heartbeat again before anything is marked. Best effort; never raises."""
    try:
        until = now + OFFLINE_AFTER
        await redis.set(QUIET_KEY, until.timestamp(), ex=int(OFFLINE_AFTER.total_seconds()) + 60)
    except Exception:  # noqa: BLE001
        log.warning("could not record the offline quiet window", exc_info=True)


async def _in_quiet_window(redis: Redis, now: datetime) -> bool:
    try:
        until = await redis.get(QUIET_KEY)
    except Exception:  # noqa: BLE001  fail toward warning people, not toward silence
        log.warning("could not read the offline quiet window", exc_info=True)
        return False
    return until is not None and now.timestamp() < float(until)


def offline_text(name: str) -> str:
    return f"📴 Sensor {name} is offline — alerts from it have stopped."


def online_text(name: str) -> str:
    return f"✅ Sensor {name} is back online."


@dataclass(frozen=True)
class Recipient:
    member_id: int
    channel: str  # "telegram" | "ntfy"
    address: str  # chat id or ntfy topic


async def household_recipients(
    session: AsyncSession, household_id: int, settings: Any = None
) -> list[Recipient]:
    """Every external channel a household's members enabled (read now; sent later)."""
    settings = settings or get_settings()
    tenant = Tenant.for_household(household_id, session)
    # Same rule as alerts: members who never confirmed their email get nothing.
    members = (
        (await session.execute(tenant.select(Member).where(Member.email_confirmed.is_(True))))
        .scalars()
        .all()
    )
    prefs = {
        p.member_id: p for p in (await session.execute(tenant.select(AlertPreference))).scalars()
    }
    out = []
    for member in members:
        p = prefs.get(member.id)  # missing row = defaults (nothing external enabled)
        if p and p.telegram and p.telegram_chat_id and settings.telegram_bot_token:
            out.append(Recipient(member.id, "telegram", p.telegram_chat_id))
        if p and p.ntfy_topic:
            out.append(Recipient(member.id, "ntfy", p.ntfy_topic))
    return out


async def send_status_message(
    recipients: list[Recipient], text: str, http: httpx.AsyncClient, settings: Any, tags: str
) -> None:
    """One attempt per channel; device status messages are best effort and never raise."""
    token = settings.telegram_bot_token
    for r in recipients:
        try:
            if r.channel == "telegram":
                await telegram.send_message(http, token, r.address, text)
            else:
                await ntfy.publish(http, settings.ntfy_base_url, r.address, text, tags=tags)
        except Exception as e:  # noqa: BLE001
            error = str(e) or type(e).__name__
            if token:
                error = error.replace(token, "***")
            log.warning(
                "device status message via %s to member %s failed: %s",
                r.channel,
                r.member_id,
                error,
            )


async def notify_back_online(recipients: list[Recipient], name: str) -> None:
    """Run after the heartbeat response: the device never waits on Telegram or ntfy."""
    if not recipients:
        return
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10) as http:
        await send_status_message(recipients, online_text(name), http, settings, "white_check_mark")


async def _notify_members(
    session: AsyncSession, household_id: int, text: str, http: httpx.AsyncClient, settings: Any
) -> None:
    recipients = await household_recipients(session, household_id, settings)
    await send_status_message(recipients, text, http, settings, "warning")


async def check_offline_devices(
    session: AsyncSession,
    redis: Redis,
    now: datetime,
    *,
    process_started_at: datetime | None = None,
    http: httpx.AsyncClient | None = None,
    settings: Any = None,
) -> list[int]:
    """Mark stale online devices offline (atomically, once) and warn their households.

    Guards against false warnings after server-side downtime, when sensors were fine but
    nobody was listening to their heartbeats:
    1. Startup grace: a checker that has itself been up for less than OFFLINE_AFTER (passed
       explicitly as `process_started_at`: the in-process loop's start, or the Celery
       worker's) marks nothing: sensors haven't had a full window to reach the restarted
       server yet.
    2. Fleet silence: if even the newest heartbeat across ALL devices is older than
       OFFLINE_AFTER, the API itself most likely wasn't receiving heartbeats (it, its DB or
       the network in front of it was down), so nothing is marked. This is the checker's
       view of the API's last-known liveness and applies in both job-runner modes.
       Bounded: once the whole fleet has been silent for more than FLEET_SILENCE_MAX, warn
       anyway. A recovered API hears from live sensors within one heartbeat interval, so a
       longer silence is real (a lone sensor died, or the home lost its internet) and
       staying quiet would leave people believing they are covered.
    3. Recovery window: whenever guard 2 trips or a check fails, QUIET_KEY records
       now + OFFLINE_AFTER, and nothing is marked until then. One sensor heartbeating
       right after an outage must not get the others marked before they had a full window
       to check in. (A lone dead sensor is therefore reported after ~13 minutes: the last
       guard trip at ~10 min plus this window.)
    """
    if http is None:
        async with httpx.AsyncClient(timeout=10) as own_http:
            return await check_offline_devices(
                session,
                redis,
                now,
                process_started_at=process_started_at,
                http=own_http,
                settings=settings,
            )
    settings = settings or get_settings()
    if process_started_at is not None and now - process_started_at < OFFLINE_AFTER:
        return []
    # Cross-household read (sanctioned Tenant bypass): only the newest timestamp is used.
    newest = await session.scalar(select(func.max(Device.last_seen_at)))
    if newest is not None and OFFLINE_AFTER < now - newest <= FLEET_SILENCE_MAX:
        log.warning("no device heartbeat since %s; not marking devices offline yet", newest)
        await open_quiet_window(redis, now)
        return []
    if await _in_quiet_window(redis, now):
        return []
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
    started_at = clock()
    async with httpx.AsyncClient(timeout=10) as http:
        while True:
            try:
                async with session_factory() as session:
                    await check_offline_devices(
                        session, redis, clock(), process_started_at=started_at, http=http
                    )
            except Exception:  # noqa: BLE001
                log.exception("offline check failed; will retry")
                await open_quiet_window(redis, clock())
            await asyncio.sleep(interval_s)
