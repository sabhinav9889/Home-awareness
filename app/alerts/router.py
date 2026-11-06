import hmac
import logging
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import telegram
from app.alerts.jobs import JobRunner, enqueue_or_fallback, get_job_runner
from app.alerts.models import Alert, Delivery
from app.auth.models import Member
from app.catalog import SOUNDS
from app.config import get_settings
from app.db import get_session
from app.devices.models import Device
from app.disclaimer import DISCLAIMER
from app.events.models import Event
from app.live.hub import publish_to_household
from app.ratelimit import by_member, rate_limit
from app.redis import get_redis
from app.rules.models import AlertPreference
from app.security import hash_token, new_token
from app.tenancy import Tenant, get_agent_tenant, get_tenant

router = APIRouter(tags=["alerts"])


class DeliveryOut(BaseModel):
    channel: str
    status: str
    attempts: int
    last_error: str | None


class AlertOut(BaseModel):
    id: int
    sound_key: str
    label: str
    device_name: str | None
    created_at: datetime
    status: str
    deliveries: list[DeliveryOut]


class TestAlertAccepted(BaseModel):
    alert_id: int


@router.get(
    "/alerts",
    response_model=list[AlertOut],
    description=f"Your own recent alerts with per-channel delivery status. {DISCLAIMER}",
)
async def list_alerts(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AlertOut]:
    rows = (
        await session.execute(
            tenant.select(Alert)
            .where(Alert.member_id == tenant.member.id)
            .add_columns(Device.name)
            .outerjoin(Event, Event.id == Alert.event_id)
            .outerjoin(
                Device,
                (Device.id == Event.device_id) & (Device.household_id == Alert.household_id),
            )
            .order_by(Alert.created_at.desc(), Alert.id.desc())
            .limit(limit)
        )
    ).all()
    by_alert: dict[int, list[Delivery]] = {a.id: [] for a, _ in rows}
    if by_alert:  # deliveries have no household_id: reach them only via the scoped alerts above
        for d in await session.scalars(
            select(Delivery).where(Delivery.alert_id.in_(by_alert)).order_by(Delivery.id)
        ):
            by_alert[d.alert_id].append(d)
    return [
        AlertOut(
            id=a.id,
            sound_key=a.sound_key,
            label=SOUNDS[a.sound_key].label if a.sound_key in SOUNDS else "Test alert",
            device_name=name,
            created_at=a.created_at,
            status=a.status,
            deliveries=[DeliveryOut(**_d(d)) for d in by_alert[a.id]],
        )
        for a, name in rows
    ]


def _d(d: Delivery) -> dict:
    return {
        "channel": d.channel,
        "status": d.status,
        "attempts": d.attempts,
        "last_error": d.last_error,
    }


@router.post(
    "/me/test-alert",
    status_code=202,
    response_model=TestAlertAccepted,
    dependencies=[Depends(rate_limit("test-alert", 3, 60, by_member))],
    description=f"Send yourself a test alert on every channel you enabled. {DISCLAIMER}",
)
async def test_alert(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    jobs: Annotated[JobRunner, Depends(get_job_runner)],
) -> TestAlertAccepted:
    alert = tenant.add(Alert(member_id=tenant.member.id, event_id=None, sound_key="test"))
    await session.flush()
    await session.commit()  # commit before enqueue: workers must see the row
    await enqueue_or_fallback(jobs, alert.id)
    return TestAlertAccepted(alert_id=alert.id)


class AckOut(BaseModel):
    alert_id: int
    event_id: int | None
    acknowledged_by: int
    acknowledged_at: datetime


@router.post(
    "/alerts/{alert_id}/ack",
    response_model=AckOut,
    responses={409: {"description": "Another member already handled this event"}},
    description=f"Tell the household 'I'm on it'. First acknowledger wins. {DISCLAIMER}",
)
async def acknowledge_alert(
    alert_id: int,
    tenant: Annotated[Tenant, Depends(get_agent_tenant)],  # ha-agent's "I'm on it" button
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
):
    alert = await tenant.get(Alert, alert_id)
    if alert.member_id != tenant.member.id:
        raise HTTPException(status_code=404, detail="not found")
    me = tenant.member
    now = datetime.now(UTC)
    target = Alert.id == alert.id
    if alert.event_id is not None:
        target = Alert.event_id == alert.event_id
    # One conditional UPDATE: concurrent acks serialize on the row locks and only one matches.
    won = (
        await session.scalars(
            tenant.update(Alert)
            .where(target, Alert.acknowledged_by.is_(None))
            .values(acknowledged_by=me.id, acknowledged_at=now)
            .returning(Alert.id)
        )
    ).all()
    if alert.id not in won:
        await session.refresh(alert)
        winner = await session.scalar(
            tenant.select(Member).where(Member.id == alert.acknowledged_by)
        )
        by = winner.email if winner else None
        await session.rollback()  # a losing caller persists nothing
        return JSONResponse(status_code=409, content={"detail": "already handled", "by": by})
    await session.commit()
    await publish_to_household(
        redis,
        tenant.household_id,
        {
            "kind": "alert_ack",
            "event_id": alert.event_id,
            "by_member_id": me.id,
            "by_email": me.email,
            "at": now.isoformat(),
        },
    )
    return AckOut(
        alert_id=alert.id, event_id=alert.event_id, acknowledged_by=me.id, acknowledged_at=now
    )


LINK_TTL_S = 600
LINKED_TEXT = "✅ Linked. You'll get Home Awareness alerts here."
EXPIRED_TEXT = "Link expired. Create a new one in the dashboard."
log = logging.getLogger(__name__)


class TelegramLinkOut(BaseModel):
    deep_link: str


@router.post(
    "/me/telegram/link",
    response_model=TelegramLinkOut,
    dependencies=[Depends(rate_limit("telegram-link", 5, 60, by_member))],
    description=f"Create a 10-minute, single-use Telegram deep link. {DISCLAIMER}",
)
async def create_telegram_link(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> TelegramLinkOut:
    settings = get_settings()
    if not settings.telegram_bot_username or not settings.telegram_bot_token:
        raise HTTPException(status_code=400, detail="Telegram is not configured")
    code = new_token("")[:24]
    await redis.set(f"tglink:{hash_token(code)}", tenant.member.id, ex=LINK_TTL_S)
    return TelegramLinkOut(deep_link=f"https://t.me/{settings.telegram_bot_username}?start={code}")


def _start_code(update_body: object) -> tuple[str, str] | None:
    """(chat_id, code) from a `/start <code>` text message; None for any other update shape."""
    if not isinstance(update_body, dict):
        return None
    msg = update_body.get("message")
    if not isinstance(msg, dict):
        return None
    chat, text = msg.get("chat"), msg.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    parts = text.split()
    if len(parts) != 2 or parts[0] != "/start":
        return None
    return str(chat_id), parts[1]


@router.post(
    "/telegram/webhook/{secret}",
    include_in_schema=False,
    description=f"Telegram bot webhook. {DISCLAIMER}",
)
async def telegram_webhook(
    secret: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, bool]:
    settings = get_settings()
    expected = settings.telegram_webhook_secret
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(status_code=404, detail="not found")
    try:
        parsed = _start_code(await request.json())
    except ValueError:
        parsed = None
    if parsed is None or not settings.telegram_bot_token:
        return {"ok": True}  # Telegram retries non-200s forever: ignore anything unexpected
    chat_id, code = parsed
    member_id = await redis.getdel(f"tglink:{hash_token(code)}")  # atomic: one use per code
    member = await session.get(Member, int(member_id)) if member_id else None
    if member is None:
        reply = EXPIRED_TEXT
    else:
        tenant = Tenant(member, session)
        # The chat belongs to whoever proved the newest code, even across households, so this
        # one write is keyed on chat_id and deliberately not household-scoped.
        await session.execute(
            update(AlertPreference)
            .where(AlertPreference.telegram_chat_id == chat_id)
            .values(telegram_chat_id=None, telegram=False)
        )
        pref = await session.scalar(
            tenant.select(AlertPreference).where(AlertPreference.member_id == member.id)
        )
        if pref is None:
            pref = tenant.add(AlertPreference(member_id=member.id))
        pref.telegram_chat_id, pref.telegram = chat_id, True
        await session.commit()
        reply = LINKED_TEXT
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await telegram.send_message(http, settings.telegram_bot_token, chat_id, reply)
    except Exception as e:  # best effort: a failed reply must never make the webhook non-200
        log.warning("telegram reply failed: %s", type(e).__name__)
    return {"ok": True}
