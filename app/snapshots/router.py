from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.disclaimer import DISCLAIMER
from app.snapshots import store
from app.snapshots.models import Snapshot
from app.tenancy import Tenant, get_agent_tenant

router = APIRouter(tags=["snapshots"])


@router.get(
    "/snapshots/{event_id}",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
    description=(
        "The camera snapshot that came with an alerting event (a JPEG), for household "
        f"members (session or agent token). {DISCLAIMER}"
    ),
)
async def get_snapshot(
    event_id: int,
    tenant: Annotated[Tenant, Depends(get_agent_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    snap = await session.scalar(tenant.select(Snapshot).where(Snapshot.event_id == event_id))
    jpeg = await store.try_load(snap)
    if jpeg is None:  # no image, other household's, deleted, or lost: all the same 404
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
