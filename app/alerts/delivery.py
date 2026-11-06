import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import update

from app.alerts import ntfy, telegram
from app.alerts.models import Alert, Delivery
from app.catalog import get_sound
from app.config import get_settings
from app.devices.models import Device
from app.events.models import Event
from app.live.hub import publish_to_member
from app.metrics import DELIVERIES
from app.rules.models import AlertPreference
from app.tenancy import Tenant

TEST_TEXT = "🔔 Test alert from Home Awareness — your alerts work."
BACKOFF = (1, 4)  # seconds slept after attempt 1 and 2; 3 attempts in total
MAX_ERROR = 500


def _default_session_factory():
    from app.db import _sessionmaker

    return _sessionmaker()()


async def _send_with_retries(
    delivery: Delivery, send: Callable[[], Awaitable[None]], sleep, secrets: tuple[str, ...]
) -> None:
    for attempt in range(1, len(BACKOFF) + 2):
        delivery.attempts = attempt
        try:
            await send()
        except Exception as e:
            error = str(e) or type(e).__name__
            for secret in secrets:
                error = error.replace(secret, "***")
            delivery.last_error = error[:MAX_ERROR]
            if attempt <= len(BACKOFF):
                await sleep(BACKOFF[attempt - 1])
        else:
            delivery.status, delivery.last_error = "sent", None
            return
    delivery.status = "failed"


async def deliver_alert(
    alert_id: int,
    *,
    session_factory: Callable[[], Any] = _default_session_factory,
    redis: Redis | None = None,
    http: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if redis is None:
        from app.redis import get_redis

        redis = await get_redis()
    if http is None:
        async with httpx.AsyncClient(timeout=10) as own_http:
            return await deliver_alert(
                alert_id, session_factory=session_factory, redis=redis, http=own_http, sleep=sleep
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
        alert = await session.get(Alert, alert_id)
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
                device = await session.scalar(
                    tenant.select(Device).where(Device.id == event.device_id)
                )
                device_name = device.name if device else device_name
        at = alert.created_at.astimezone(UTC)
        if alert.event_id is None:
            label, text = "Test alert", TEST_TEXT
        else:
            label = get_sound(alert.sound_key).label
            text = f"⚠️ {label} detected by {device_name} at {at:%H:%M} UTC — Home Awareness"

        Send = Callable[[], Awaitable[None]]
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

        for d, send in plan:
            if send is None:
                d.status, d.last_error = "failed", "telegram bot token not configured"
            elif d.channel == "websocket":
                d.attempts = 1  # no retries: "sent" means published
                try:
                    await send()
                    d.status = "sent"
                except Exception as e:
                    d.status, d.last_error = "failed", (str(e) or type(e).__name__)[:MAX_ERROR]
            else:
                await _send_with_retries(d, send, sleep, (token,) if token else ())
            await session.commit()  # persist each channel's outcome before the next send
            DELIVERIES.labels(d.channel, d.status).inc()

        ok = sum(d.status == "sent" for d, _ in plan)
        if ok == 0:
            alert.status = "failed"  # includes "no channel enabled": nothing reached anyone
        else:
            alert.status = "sent" if ok == len(plan) else "partial"
        await session.commit()
