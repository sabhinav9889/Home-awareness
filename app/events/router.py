import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.alerts.jobs import JobRunner, enqueue_or_fallback, get_job_runner
from app.catalog import get_sound
from app.db import get_session
from app.devices.deps import current_device, get_device_tenant
from app.devices.models import Device
from app.disclaimer import DISCLAIMER
from app.events.models import Event
from app.events.schemas import EventAccepted, EventIn, EventOut
from app.live.hub import publish_to_household
from app.metrics import ALERTS_CREATED, EVENTS_MERGED, EVENTS_RECEIVED
from app.ratelimit import by_device_key, enforce, rate_limit
from app.redis import get_redis
from app.rules.engine import RuleOutcome, process_event
from app.rules.incidents import FIRED, NEW_FILES, NX_KEYS, check_incidents
from app.snapshots import store
from app.tenancy import Tenant, get_tenant

router = APIRouter(tags=["events"])
log = logging.getLogger(__name__)

HOUSEHOLD_EVENTS_PER_MINUTE = 300
# Hard limit for a whole multipart request. Bigger is refused (413, or 411 with no
# Content-Length); anything under it is parsed, and an image over 300 KB is dropped with the
# event kept. 8 MB covers a realistic phone JPEG so those are dropped, not refused.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_EVENT_PART_BYTES = 64 * 1024  # the JSON part; Starlette applies this to non-file fields


async def _read_event(request: Request) -> tuple[EventIn, bytes | None]:
    """The event JSON, plus the optional snapshot bytes of a multipart upload. A plain JSON
    body (current sensors) works exactly as before; multipart carries the same JSON in an
    `event` part and the JPEG in a `snapshot` part."""
    is_multipart = request.headers.get("content-type", "").startswith("multipart/form-data")
    jpeg: bytes | None = None
    try:
        if is_multipart:
            # Content-Length is trustworthy (the server stops reading at it), so requiring it
            # bounds chunked uploads too: no length, no parse (411). Streaming a byte count
            # would have worked as well, but needs a re-implementation of the form parse.
            length = request.headers.get("content-length")
            if not length or not length.isdigit():
                raise HTTPException(status_code=411, detail="Content-Length required")
            if int(length) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="upload too large")
            async with request.form(
                max_part_size=MAX_EVENT_PART_BYTES
            ) as form:  # closes temp files
                part = form.get("event")
                if isinstance(part, UploadFile):
                    part = (await part.read(MAX_EVENT_PART_BYTES)).decode("utf-8", "replace")
                if not isinstance(part, str):
                    raise RequestValidationError(
                        [{"type": "missing", "loc": ("body", "event"), "msg": "Field required"}]
                    )
                image = form.get("snapshot")
                if isinstance(image, UploadFile):
                    jpeg = await image.read(store.MAX_BYTES + 1)  # +1: detect "too big"
            raw = part.encode()
        else:
            raw = await request.body()
        return EventIn.model_validate_json(raw), jpeg
    except ValidationError as e:
        errors = e.errors(include_url=False, include_context=False, include_input=False)
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in errors]
        ) from None


async def _household_event_limit(
    device: Annotated[Device, Depends(current_device)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """Spec §9: a household's sensors together send at most 300 events a minute (runs after
    device auth; FastAPI reuses the same current_device result for the route)."""
    await enforce(
        redis, "events-household", str(device.household_id), HOUSEHOLD_EVENTS_PER_MINUTE, 60
    )


@router.post(
    "/device/events",
    status_code=202,
    response_model=EventAccepted,
    dependencies=[
        Depends(rate_limit("events", 60, 60, by_device_key)),
        Depends(_household_event_limit),
    ],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": EventIn.model_json_schema()},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["event"],
                        "properties": {
                            "event": {"type": "string", "description": "The event JSON."},
                            "snapshot": {
                                "type": "string",
                                "format": "binary",
                                "description": "Optional JPEG, at most 300 KB.",
                            },
                        },
                    }
                },
            },
        }
    },
    description=(
        "Report a detected sound (type, confidence, time; never audio). Send JSON, or "
        "multipart with the same JSON in `event` and an optional `snapshot` JPEG (kept only "
        f"from cameras that may send snapshots). {DISCLAIMER}"
    ),
)
async def post_event(
    request: Request,
    device: Annotated[Device, Depends(current_device)],
    tenant: Annotated[Tenant, Depends(get_device_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    jobs: Annotated[JobRunner, Depends(get_job_runner)],
) -> EventAccepted:
    body, jpeg = await _read_event(request)
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
    snapshot = None
    if jpeg is not None and "video" in device.capabilities.split(",") and device.send_snapshots:
        snapshot = await store.save(session, tenant.household_id, event.id, jpeg)
    outcome: RuleOutcome = await process_event(session, redis, event, time.time())
    event.merged = outcome.merged
    try:
        await session.commit()
    except BaseException:
        store.forget_deferred_files(session)
        if snapshot is not None:
            await store.discard_file(snapshot)
        raise
    await store.delete_deferred_files(session)  # files of cap-evicted snapshots
    EVENTS_RECEIVED.labels(event.sound_key).inc()
    if event.merged:
        EVENTS_MERGED.inc()
    if outcome.alert_ids:
        ALERTS_CREATED.labels(event.sound_key).inc(len(outcome.alert_ids))
    for alert_id in outcome.alert_ids:  # after commit: workers must see the rows
        await enqueue_or_fallback(jobs, alert_id)
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
            "has_snapshot": snapshot is not None,
        },
    )
    # Read before the incident step: a rollback there expires `event`.
    accepted = EventAccepted(event_id=event.id, merged=event.merged, alerts=len(outcome.alert_ids))
    await _raise_incidents(session, redis, jobs, event)
    return accepted


async def _raise_incidents(
    session: AsyncSession, redis: Redis, jobs: JobRunner, event: Event
) -> None:
    """Follow-up incidents, after the ingredient is committed and enqueued. Own commit, own
    enqueue; any failure is logged by type only and never touches the ingredient's response
    (callers must not read `event` afterwards: a rollback expires it)."""
    session.info.pop(FIRED, None)
    session.info.pop(NEW_FILES, None)
    session.info.pop(NX_KEYS, None)
    try:
        alert_ids = await check_incidents(session, redis, event, time.time())
        if not session.info.pop(FIRED, False):
            return  # nothing was raised: nothing to commit
        await session.commit()
        session.info.pop(NEW_FILES, None)
        session.info.pop(NX_KEYS, None)
        await store.delete_deferred_files(session)
        for alert_id in alert_ids:
            ALERTS_CREATED.labels("incident").inc()
            await enqueue_or_fallback(jobs, alert_id)
    except Exception as e:  # noqa: BLE001
        log.warning("incident check failed (%s)", type(e).__name__)
        await _undo_incidents(session, redis)


async def _undo_incidents(session: AsyncSession, redis: Redis) -> None:
    """Roll back an incident attempt: drop its rows, its copied files and the once-per-60 s
    keys it set, so the next completing event can still raise the incident."""
    session.info.pop(FIRED, None)
    store.forget_deferred_files(session)
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        pass
    for path in session.info.pop(NEW_FILES, []):
        await store.remove_path(path)
    keys = session.info.pop(NX_KEYS, [])
    if keys:
        try:
            await redis.delete(*keys)
        except Exception:  # noqa: BLE001
            pass


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
