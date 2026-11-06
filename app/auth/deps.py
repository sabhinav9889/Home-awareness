from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Member
from app.db import get_session
from app.security import InvalidToken, decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def member_from_token(session: AsyncSession, token: str) -> Member | None:
    """The confirmed, existing member a valid access token names; None otherwise."""
    try:
        claims = decode_access_token(token)
    except InvalidToken:
        return None
    member = await session.get(Member, claims.member_id)
    if member is None or not member.email_confirmed or member.household_id != claims.household_id:
        return None
    return member


async def current_member(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Member:
    member = None if creds is None else await member_from_token(session, creds.credentials)
    if member is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return member
