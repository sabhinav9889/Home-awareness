from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AgentToken, Member
from app.db import get_session
from app.security import InvalidToken, decode_access_token, hash_token

_bearer = HTTPBearer(auto_error=False)

AGENT_TOKEN_PREFIX = "ha_agent_"
LAST_USED_EVERY = timedelta(seconds=60)


@dataclass(frozen=True)
class Principal:
    """Who is calling, and with what: a session JWT or a long-lived agent token."""

    member: Member
    kind: Literal["jwt", "agent"]
    agent_token_id: int | None = None


async def member_from_token(session: AsyncSession, token: str) -> Member | None:
    """The confirmed, existing member a valid access token (JWT) names; None otherwise."""
    try:
        claims = decode_access_token(token)
    except InvalidToken:
        return None
    member = await session.get(Member, claims.member_id)
    if member is None or not member.email_confirmed or member.household_id != claims.household_id:
        return None
    return member


async def _member_from_agent_token(session: AsyncSession, raw: str) -> Principal | None:
    row = await session.scalar(
        select(AgentToken).where(
            AgentToken.token_hash == hash_token(raw), AgentToken.revoked_at.is_(None)
        )
    )
    if row is None:
        return None
    member = await session.get(Member, row.member_id)
    if member is None or not member.email_confirmed or member.household_id != row.household_id:
        return None
    now = datetime.now(UTC)
    if row.last_used_at is None or now - row.last_used_at >= LAST_USED_EVERY:
        # Throttled bookkeeping: at most one write per token per minute. The caller commits.
        await session.execute(
            update(AgentToken)
            .where(AgentToken.id == row.id)
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
    return Principal(member, "agent", row.id)


async def principal_from_token(session: AsyncSession, raw: str) -> Principal | None:
    """Resolve a bearer token. Agent tokens are recognised by their prefix (a JWT never
    starts with it); everything else is checked as a JWT."""
    if raw.startswith(AGENT_TOKEN_PREFIX):
        return await _member_from_agent_token(session, raw)
    member = await member_from_token(session, raw)
    return None if member is None else Principal(member, "jwt")


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="invalid credentials")


async def current_member(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Member:
    """Every HTTP route: a session JWT only. Agent tokens are refused here (401)."""
    member = None if creds is None else await member_from_token(session, creds.credentials)
    if member is None:
        raise _unauthorized()
    return member


async def current_member_or_agent(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Member:
    """Only for the routes `ha-agent` needs (POST /alerts/{id}/ack): a JWT or an agent token."""
    principal = None if creds is None else await principal_from_token(session, creds.credentials)
    if principal is None:
        raise _unauthorized()
    await session.commit()  # persist a throttled last_used_at even if the route rolls back
    return principal.member
