from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Member
from app.db import get_session
from app.security import InvalidToken, decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def current_member(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Member:
    unauthorized = HTTPException(status_code=401, detail="invalid credentials")
    if creds is None:
        raise unauthorized
    try:
        claims = decode_access_token(creds.credentials)
    except InvalidToken:
        raise unauthorized from None
    member = await session.get(Member, claims.member_id)
    if member is None or not member.email_confirmed or member.household_id != claims.household_id:
        raise unauthorized
    return member
