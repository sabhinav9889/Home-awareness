from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Member
from app.config import get_settings
from app.db import get_session
from app.disclaimer import DISCLAIMER
from app.households.models import Household, Invite
from app.households.schemas import HouseholdOut, HouseholdPatch, InviteOut, MemberOut
from app.live.hub import publish_disconnect, publish_to_household
from app.redis import get_redis
from app.security import hash_token, new_token
from app.tenancy import Tenant, get_tenant

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
        timezone=household.timezone,
        away=household.away,
        night_start=household.night_start,
        night_end=household.night_end,
        snapshot_retention_days=household.snapshot_retention_days,
        members=[MemberOut(id=m.id, email=m.email, role=m.role) for m in members],
    )


@router.patch(
    "/me",
    response_model=HouseholdOut,
    description=(
        "Any member toggles Away mode; the owner sets the timezone, night hours and snapshot "
        f"retention. {DISCLAIMER}"
    ),
)
async def patch_household(
    body: HouseholdPatch,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> HouseholdOut:
    if body.owner_fields:
        _require_owner(tenant)
    household = await session.get(Household, tenant.household_id)
    sent = body.model_fields_set
    if body.timezone is not None:
        household.timezone = body.timezone
    if "night_start" in sent or "night_end" in sent:
        household.night_start = body.night_start
        household.night_end = body.night_end
    if body.snapshot_retention_days is not None:
        household.snapshot_retention_days = body.snapshot_retention_days
    away_changed = body.away is not None and body.away != household.away
    if body.away is not None:
        household.away = body.away
    await session.commit()
    if away_changed:
        await publish_to_household(
            redis,
            tenant.household_id,
            {"kind": "guarding", "away": household.away, "by_email": tenant.member.email},
        )
    return await get_household(tenant, session)


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
    url = f"{get_settings().public_base_url}/app/#/join?invite={token}"
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
