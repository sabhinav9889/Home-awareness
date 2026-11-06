import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC
from typing import Any

import httpx
from redis.asyncio import Redis

from app.alerts import ntfy, telegram
from app.alerts.models import Alert, Delivery
from app.catalog import get_sound
from app.config import get_settings
from app.devices.models import Device
from app.events.models import Event
from app.live.hub import publish_to_member
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
        alert = await session.get(Alert, alert_id)
        if alert is None or alert.status != "pending":
            return  # duplicate enqueue / redelivery: never send twice
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
            text = f"⚠️ {label} detected by {device_name} at {at:%H:%M} — Home Awareness"

        deliveries: list[Delivery] = []

        def new(channel: str) -> Delivery:
            d = Delivery(alert_id=alert.id, channel=channel, status="pending", attempts=0)
            session.add(d)
            deliveries.append(d)
            return d

        if popup or alarm:
            d = new("websocket")
            d.attempts = 1
            try:
                await publish_to_member(
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
                )
                d.status = "sent"
            except Exception as e:
                d.status, d.last_error = "failed", (str(e) or type(e).__name__)[:MAX_ERROR]

        token = settings.telegram_bot_token
        if chat_id:
            d = new("telegram")
            if not token:
                d.status, d.last_error = "failed", "telegram bot token not configured"
            else:
                await _send_with_retries(
                    d, lambda: telegram.send_message(http, token, chat_id, text), sleep, (token,)
                )
        if topic:
            d = new("ntfy")
            await _send_with_retries(
                d, lambda: ntfy.publish(http, settings.ntfy_base_url, topic, text), sleep, ()
            )

        ok = sum(d.status == "sent" for d in deliveries)
        if ok == 0:
            alert.status = "failed"  # includes "no channel enabled": nothing reached anyone
        else:
            alert.status = "sent" if ok == len(deliveries) else "partial"
        await session.commit()
