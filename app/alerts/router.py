from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.jobs import JobRunner, get_job_runner
from app.alerts.models import Alert, Delivery
from app.catalog import SOUNDS
from app.db import get_session
from app.devices.models import Device
from app.events.models import Event
from app.ratelimit import by_member, rate_limit
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
