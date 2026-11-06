from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.jobs import JobRunner, get_job_runner
from app.alerts.models import Alert, Delivery
from app.auth.models import Member
from app.catalog import SOUNDS
from app.db import get_session
from app.devices.models import Device
from app.events.models import Event
from app.live.hub import publish_to_household
from app.ratelimit import by_member, rate_limit
from app.redis import get_redis
from app.tenancy import Tenant, get_tenant

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."

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
    await jobs.enqueue_alert(alert.id)
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
    tenant: Annotated[Tenant, Depends(get_tenant)],
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
