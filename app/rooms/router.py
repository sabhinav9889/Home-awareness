from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.devices.models import Device
from app.disclaimer import DISCLAIMER
from app.rooms.models import PRIVATE_ROOM_KINDS, Room
from app.rooms.schemas import RoomIn, RoomOut, RoomPatch
from app.tenancy import Tenant, get_tenant

router = APIRouter(prefix="/rooms", tags=["rooms"])


def _require_owner(tenant: Tenant) -> None:
    if tenant.member.role != "owner":
        raise HTTPException(status_code=403, detail="owner only")


async def _commit_unique(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail="a room with that name already exists") from e


@router.get(
    "", response_model=list[RoomOut], description=f"List the household's rooms. {DISCLAIMER}"
)
async def list_rooms(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Room]:
    return list((await session.scalars(tenant.select(Room).order_by(Room.id))).all())


@router.post(
    "",
    status_code=201,
    response_model=RoomOut,
    description=f"Owner creates a room. {DISCLAIMER}",
)
async def create_room(
    body: RoomIn,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Room:
    _require_owner(tenant)
    room = tenant.add(Room(name=body.name, kind=body.kind))
    await _commit_unique(session)
    return room


@router.patch(
    "/{room_id}",
    response_model=RoomOut,
    description=f"Owner renames or retypes a room. {DISCLAIMER}",
)
async def patch_room(
    room_id: int,
    body: RoomPatch,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Room:
    room = await tenant.get(Room, room_id)
    _require_owner(tenant)
    if body.name is not None:
        room.name = body.name
    if body.kind is not None:
        room.kind = body.kind
        if body.kind in PRIVATE_ROOM_KINDS:
            # Ruling 5a-R10: video devices already in the room stop sending snapshots.
            await session.execute(
                tenant.update(Device)
                .where(Device.room_id == room_id, Device.capabilities.like("%video%"))
                .values(send_snapshots=False)
            )
    await _commit_unique(session)
    return room


@router.delete(
    "/{room_id}",
    status_code=204,
    description=f"Owner deletes a room; its devices become unassigned. {DISCLAIMER}",
)
async def delete_room(
    room_id: int,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    room = await tenant.get(Room, room_id)
    _require_owner(tenant)
    # Explicit (not only ON DELETE SET NULL) so already-loaded Device objects stay in step.
    await session.execute(
        tenant.update(Device).where(Device.room_id == room_id).values(room_id=None)
    )
    await session.delete(room)
    await session.commit()
    return Response(status_code=204)
