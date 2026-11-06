"""Long-lived, revocable tokens for `ha-agent` (the laptop alert agent).

A session JWT expires after a day; an agent left running would then silently stop getting
alarms. An agent token never expires but is narrow: it authenticates only `WS /ws` and
`POST /alerts/{id}/ack` (everything else answers 401), and it can be revoked here. Revoking
does not cut an already open socket; that socket's next (re)connect fails with 4401.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AGENT_TOKEN_PREFIX
from app.auth.models import AgentToken, Member
from app.db import get_session
from app.disclaimer import DISCLAIMER
from app.ratelimit import by_member, rate_limit
from app.security import hash_token, new_token
from app.tenancy import Tenant, get_tenant

MAX_ACTIVE_PER_MEMBER = 10

router = APIRouter(tags=["auth"])


class AgentTokenIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str) -> str:
        if any(not c.isprintable() for c in v):
            raise ValueError("name must not contain control characters")
        return v


class AgentTokenCreated(BaseModel):
    id: int
    token: str


class AgentTokenOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None


@router.post(
    "/me/agent-tokens",
    status_code=201,
    response_model=AgentTokenCreated,
    responses={409: {"description": "Already 10 active agent tokens: revoke one first"}},
    dependencies=[Depends(rate_limit("agent-token", 10, 60, by_member))],
    description=(
        "Create a long-lived token for ha-agent. It is shown only in this response and "
        f"works only for the live socket and acknowledging alerts. {DISCLAIMER}"
    ),
)
async def create_agent_token(
    body: AgentTokenIn,
    tenant: Annotated[Tenant, Depends(get_tenant)],  # JWT only: an agent token can't mint more
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentTokenCreated:
    me = tenant.member
    # Lock the member row so concurrent creates can't both pass the cap.
    # The same read checks the JWT's version: a reset that committed while we waited on the
    # lock must not let a pre-reset session mint a token that outlives it.
    version = await session.scalar(
        select(Member.token_version).where(Member.id == me.id).with_for_update()
    )
    if version != me.token_version:
        raise HTTPException(status_code=401, detail="invalid credentials")
    active = await session.scalar(
        tenant.select(AgentToken)
        .where(AgentToken.member_id == me.id, AgentToken.revoked_at.is_(None))
        .with_only_columns(func.count())
    )
    if active >= MAX_ACTIVE_PER_MEMBER:
        raise HTTPException(status_code=409, detail="too many agent tokens: revoke one first")
    token = new_token(AGENT_TOKEN_PREFIX)
    row = tenant.add(AgentToken(member_id=me.id, name=body.name, token_hash=hash_token(token)))
    await session.flush()
    await session.commit()
    return AgentTokenCreated(id=row.id, token=token)


@router.get(
    "/me/agent-tokens",
    response_model=list[AgentTokenOut],
    description=f"Your active agent tokens (never the tokens themselves). {DISCLAIMER}",
)
async def list_agent_tokens(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AgentTokenOut]:
    rows = await session.scalars(
        tenant.select(AgentToken)
        .where(AgentToken.member_id == tenant.member.id, AgentToken.revoked_at.is_(None))
        .order_by(AgentToken.id)
    )
    return [
        AgentTokenOut(id=r.id, name=r.name, created_at=r.created_at, last_used_at=r.last_used_at)
        for r in rows
    ]


@router.delete(
    "/me/agent-tokens/{token_id}",
    status_code=204,
    description=(
        "Revoke an agent token. Its next connection is refused; an already open live "
        f"socket keeps running until it reconnects. {DISCLAIMER}"
    ),
)
async def revoke_agent_token(
    token_id: int,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    row = await tenant.get(AgentToken, token_id)
    if row.member_id != tenant.member.id or row.revoked_at is not None:
        raise HTTPException(status_code=404, detail="not found")
    row.revoked_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=204)
