import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.alerts import ntfy, telegram
from app.alerts.models import Alert, Delivery
from app.catalog import get_sound
from app.config import get_settings
from app.devices.models import Device
from app.events.models import Event
from app.households.models import Household
from app.households.timezone import tz_label
from app.live.hub import publish_to_member
from app.metrics import DELIVERIES
from app.rules.models import AlertPreference
from app.tenancy import Tenant

TEST_TEXT = "🔔 Test alert from Home Awareness — your alerts work."
BACKOFF = (1, 4)  # seconds slept after attempt 1 and 2; 3 attempts in total
MAX_ERROR = 500
NO_LISTENER = "no live listener"
ABORTED = "delivery aborted"
log = logging.getLogger(__name__)
# An alert this old is dropped, not sent: an hour-late "scream" alarm is worse than none.
MAX_ALERT_AGE = timedelta(minutes=10)


def _default_session_factory():
    from app.db import _sessionmaker

    return _sessionmaker()()


@dataclass
class Outcome:
    """A channel's result as plain data. Send tasks return this and never touch ORM rows:
    mutating a session-attached object while another task's flush is in flight can be lost."""

    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None

    def apply(self, d: Delivery) -> None:
        d.status, d.attempts, d.last_error = self.status, self.attempts, self.last_error


async def _send_with_retries(
    send: Callable[[], Awaitable[object]], sleep, secrets: tuple[str, ...]
) -> Outcome:
    out = Outcome()
    for attempt in range(1, len(BACKOFF) + 2):
        out.attempts = attempt
        try:
            await send()
        except Exception as e:
            error = str(e) or type(e).__name__
            for secret in secrets:
                error = error.replace(secret, "***")
            out.last_error = error[:MAX_ERROR]
            if attempt <= len(BACKOFF):
                await sleep(BACKOFF[attempt - 1])
        else:
            out.status, out.last_error = "sent", None
            return out
    out.status = "failed"
    return out


async def deliver_alert(
    alert_id: int,
    *,
    session_factory: Callable[[], Any] = _default_session_factory,
    redis: Redis | None = None,
    http: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if redis is None:
        from app.redis import get_redis

        redis = await get_redis()
    if http is None:
        async with httpx.AsyncClient(timeout=10) as own_http:
            return await deliver_alert(
                alert_id,
                session_factory=session_factory,
                redis=redis,
                http=own_http,
                sleep=sleep,
                clock=clock,
            )
    settings = get_settings()
    async with session_factory() as session:
        # At-most-once: atomically claim the alert and commit before sending anything. A
        # duplicate enqueue or a retry after a crash finds it no longer pending and does
        # nothing (a lost alert beats repeated alarms; external channels can't be exactly-once).
        claimed = await session.scalar(
            update(Alert)
            .where(Alert.id == alert_id, Alert.status == "pending")
            .values(status="delivering")
            .returning(Alert.id)
        )
        await session.commit()
        if claimed is None:
            return
        try:
            await _deliver_claimed(session, alert_id, redis, http, sleep, clock, settings)
        except Exception:  # noqa: BLE001  never leave a claimed alert "delivering" invisibly
            log.error("delivery of alert %s failed", alert_id, exc_info=True)
            await _abort(session, alert_id)


async def _abort(session: Any, alert_id: int) -> None:
    """After an unexpected error: anything not yet sent is failed, and so is the alert."""
    try:
        await session.rollback()
        await session.execute(
            update(Delivery)
            .where(Delivery.alert_id == alert_id, Delivery.status == "pending")
            .values(status="failed", last_error=ABORTED)
        )
        await session.execute(
            update(Alert)
            .where(Alert.id == alert_id, Alert.status == "delivering")
            .values(status="failed")
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        log.error("could not mark alert %s failed", alert_id, exc_info=True)


async def _deliver_claimed(
    session: Any,
    alert_id: int,
    redis: Redis,
    http: httpx.AsyncClient,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], datetime],
    settings: Any,
) -> None:
    alert = await session.get(Alert, alert_id)
    if clock() - alert.created_at > MAX_ALERT_AGE:
        alert.status = "failed"  # stale (queue backlog, worker down): send nothing
        await session.commit()
        log.warning("alert %s dropped: older than %s", alert_id, MAX_ALERT_AGE)
        return
    tenant = Tenant.for_household(alert.household_id, session)
    prefs = await session.scalar(
        tenant.select(AlertPreference).where(AlertPreference.member_id == alert.member_id)
    )
    popup, alarm = (prefs.popup, prefs.alarm) if prefs else (True, False)
    chat_id = prefs.telegram_chat_id if prefs and prefs.telegram else None
    topic = prefs.ntfy_topic if prefs else None

    device_name = "a removed sensor"
    if alert.event_id is not None:
        event = await session.scalar(tenant.select(Event).where(Event.id == alert.event_id))
        if event is not None and event.device_id is not None:
            device = await session.scalar(tenant.select(Device).where(Device.id == event.device_id))
            device_name = device.name if device else device_name
    at = alert.created_at.astimezone(UTC)
    household_tz = await session.scalar(
        select(Household.timezone).where(Household.id == alert.household_id)
    )
    if alert.event_id is None:
        label, text = "Test alert", TEST_TEXT
    else:
        label = get_sound(alert.sound_key).label
        hhmm, tz = tz_label(at, household_tz)
        text = f"⚠️ {label} detected by {device_name} at {hhmm} {tz} — Home Awareness"

    Send = Callable[[], Awaitable[object]]
    plan: list[tuple[Delivery, Send | None]] = []  # None: nothing to send (pre-failed)

    def new(channel: str, send: Send | None) -> None:
        d = Delivery(alert_id=alert.id, channel=channel, status="pending", attempts=0)
        session.add(d)
        plan.append((d, send))

    token = settings.telegram_bot_token
    if popup or alarm:
        new(
            "websocket",
            lambda: publish_to_member(
                redis,
                alert.member_id,
                {
                    "kind": "alert",
                    "alert_id": alert.id,
                    "event_id": alert.event_id,  # matches alert_ack.event_id
                    "sound_key": alert.sound_key,
                    "label": label,
                    "device_name": device_name,
                    "at": at.isoformat(),
                    "popup": popup,
                    "alarm": alarm,
                },
            ),
        )
    if chat_id:
        new(
            "telegram",
            (lambda: telegram.send_message(http, token, chat_id, text)) if token else None,
        )
    if topic:
        new("ntfy", lambda: ntfy.publish(http, settings.ntfy_base_url, topic, text))
    await session.commit()  # every Delivery row exists (pending) before anything is sent

    # Websocket first (instant, local), plus channels that can't be sent at all.
    external: list[tuple[Delivery, Send]] = []
    for d, send in plan:
        if send is None:
            d.status, d.last_error = "failed", "telegram bot token not configured"
        elif d.channel == "websocket":
            # No retries. "sent" means at least one live dashboard received it: a popup or
            # alarm nobody is listening for reached no one, so it must not count as sent.
            d.attempts = 1
            try:
                listeners = await send()
                if listeners:
                    d.status = "sent"
                else:
                    d.status, d.last_error = "failed", NO_LISTENER
            except Exception as e:
                d.status, d.last_error = "failed", (str(e) or type(e).__name__)[:MAX_ERROR]
        else:
            external.append((d, send))
            continue
        await session.commit()
        DELIVERIES.labels(d.channel, d.status).inc()

    # External channels run concurrently, so a slow Telegram (up to ~35 s with retries)
    # never holds back ntfy. Send tasks only return plain Outcomes; this task alone applies
    # each to its Delivery row and commits it, one at a time, as results complete.
    secrets = (token,) if token else ()

    async def run(i: int, send: Send) -> tuple[int, Outcome]:
        return i, await _send_with_retries(send, sleep, secrets)

    tasks = [asyncio.create_task(run(i, send)) for i, (_, send) in enumerate(external)]
    crash: Exception | None = None
    try:
        for done in asyncio.as_completed(tasks):
            try:
                i, outcome = await done
            except Exception as e:  # noqa: BLE001  re-raised below, after the others finish
                crash = crash or e
                continue  # left pending: the abort path marks it failed
            d = external[i][0]
            outcome.apply(d)
            await session.commit()
            DELIVERIES.labels(d.channel, d.status).inc()
    finally:
        # If a commit (or anything here) raised, stop the remaining sends before the abort
        # path marks the alert failed: nothing may go out after that.
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    if crash is not None:
        raise crash

    ok = sum(d.status == "sent" for d, _ in plan)
    if ok == 0:
        alert.status = "failed"  # includes "no channel enabled": nothing reached anyone
    else:
        alert.status = "sent" if ok == len(plan) else "partial"
    await session.commit()
