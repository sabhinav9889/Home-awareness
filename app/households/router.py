from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Member
from app.config import get_settings
from app.db import get_session
from app.households.models import Household, Invite
from app.households.schemas import HouseholdOut, InviteOut, MemberOut
from app.live.hub import publish_disconnect
from app.redis import get_redis
from app.security import hash_token, new_token
from app.tenancy import Tenant, get_tenant

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."
INVITE_TTL = timedelta(days=7)

router = APIRouter(prefix="/households", tags=["households"])


def _require_owner(tenant: Tenant) -> None:
    if tenant.member.role != "owner":
        raise HTTPException(status_code=403, detail="owner only")


@router.get("/me", response_model=HouseholdOut, description=f"Your household. {DISCLAIMER}")
async def get_household(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HouseholdOut:
    household = await session.get(Household, tenant.household_id)
    members = (await session.scalars(tenant.select(Member).order_by(Member.id))).all()
    return HouseholdOut(
        id=household.id,
        name=household.name,
        members=[MemberOut(id=m.id, email=m.email, role=m.role) for m in members],
    )


@router.post(
    "/me/invites",
    status_code=201,
    response_model=InviteOut,
    description=f"Owner creates a 7-day, single-use invite link. {DISCLAIMER}",
)
async def create_invite(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InviteOut:
    _require_owner(tenant)
    token = new_token("iv_")
    expires_at = datetime.now(UTC) + INVITE_TTL
    tenant.add(
        Invite(token_hash=hash_token(token), created_by=tenant.member.id, expires_at=expires_at)
    )
    await session.commit()
    url = f"{get_settings().public_base_url}/join?invite={token}"
    return InviteOut(invite_url=url, expires_at=expires_at)


@router.delete(
    "/me/members/{member_id}",
    status_code=204,
    description=f"Owner removes another member. {DISCLAIMER}",
)
async def remove_member(
    member_id: int,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> Response:
    _require_owner(tenant)
    if member_id == tenant.member.id:
        raise HTTPException(status_code=400, detail="owner cannot remove themselves")
    await tenant.delete(Member, member_id)
    await session.commit()
    await publish_disconnect(redis, [member_id])
    return Response(status_code=204)


@router.post(
    "/me/leave",
    status_code=204,
    description=f"A non-owner member leaves the household. {DISCLAIMER}",
)
async def leave(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> Response:
    if tenant.member.role == "owner":
        raise HTTPException(status_code=400, detail="owner cannot leave; delete the household")
    member_id = tenant.member.id
    await tenant.delete(Member, member_id)
    await session.commit()
    await publish_disconnect(redis, [member_id])
    return Response(status_code=204)


@router.delete(
    "/me",
    status_code=204,
    description=f"Owner deletes the household and everything in it. {DISCLAIMER}",
)
async def delete_household(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> Response:
    _require_owner(tenant)
    member_ids = [m.id for m in (await session.scalars(tenant.select(Member))).all()]
    # Core DELETE: the database's ON DELETE CASCADE removes every dependent row.
    await session.execute(delete(Household).where(Household.id == tenant.household_id))
    await session.commit()
    session.expunge_all()
    await publish_disconnect(redis, member_ids)
    return Response(status_code=204)
