import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.jobs import JobRunner, get_job_runner
from app.catalog import get_sound
from app.db import get_session
from app.devices.deps import current_device, get_device_tenant
from app.devices.models import Device
from app.events.models import Event
from app.events.schemas import EventAccepted, EventIn, EventOut
from app.live.hub import publish_to_household
from app.ratelimit import by_device_key, rate_limit
from app.redis import get_redis
from app.rules.engine import RuleOutcome, process_event
from app.tenancy import Tenant, get_tenant

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."

router = APIRouter(tags=["events"])


@router.post(
    "/device/events",
    status_code=202,
    response_model=EventAccepted,
    dependencies=[Depends(rate_limit("events", 60, 60, by_device_key))],
    description=f"Report a detected sound (type, confidence, time; never audio). {DISCLAIMER}",
)
async def post_event(
    body: EventIn,
    device: Annotated[Device, Depends(current_device)],
    tenant: Annotated[Tenant, Depends(get_device_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    jobs: Annotated[JobRunner, Depends(get_job_runner)],
) -> EventAccepted:
    now = datetime.now(UTC)
    event = tenant.add(
        Event(
            device_id=device.id,
            sound_key=body.type,
            confidence=body.confidence,
            occurred_at=body.occurred_at,
            received_at=now,
        )
    )
    await session.flush()
    outcome: RuleOutcome = await process_event(session, redis, event, time.time())
    event.merged = outcome.merged
    await session.commit()
    for alert_id in outcome.alert_ids:  # after commit: workers must see the rows
        await jobs.enqueue_alert(alert_id)
    await publish_to_household(
        redis,
        tenant.household_id,
        {
            "kind": "event",
            "event_id": event.id,
            "sound_key": event.sound_key,
            "label": get_sound(event.sound_key).label,
            "device_id": device.id,
            "device_name": device.name,
            "confidence": event.confidence,
            "received_at": now.isoformat(),
            "merged": event.merged,
        },
    )
    return EventAccepted(event_id=event.id, merged=event.merged, alerts=len(outcome.alert_ids))


@router.get(
    "/events",
    response_model=list[EventOut],
    description=f"Recent sound events for this household, newest first. {DISCLAIMER}",
)
async def list_events(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: int | None = None,
) -> list[EventOut]:
    query = (
        tenant.select(Event)
        .add_columns(Device.name)
        .outerjoin(
            Device, (Device.id == Event.device_id) & (Device.household_id == Event.household_id)
        )
        .order_by(Event.received_at.desc(), Event.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        query = query.where(Event.id < before_id)
    return [
        EventOut(
            id=e.id,
            sound_key=e.sound_key,
            label=get_sound(e.sound_key).label,
            device_name=name,
            confidence=e.confidence,
            received_at=e.received_at,
            merged=e.merged,
        )
        for e, name in (await session.execute(query)).all()
    ]
