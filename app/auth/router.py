from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_member
from app.auth.models import EmailToken, Member
from app.auth.schemas import ConfirmIn, LoginIn, MeOut, SignupIn, SignupOut, TokenOut
from app.config import get_settings
from app.db import get_session
from app.email import EmailSender, get_email_sender
from app.households.models import Household
from app.ratelimit import by_ip, rate_limit
from app.security import (
    create_access_token,
    hash_password,
    hash_token,
    new_token,
    verify_password,
)

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."
CONFIRM_TTL = timedelta(hours=24)

router = APIRouter(prefix="/auth", tags=["auth"])
_signup_limit = rate_limit("signup", 10, 60, by_ip)
_login_limit = rate_limit("login", 10, 60, by_ip)
# Hash verified against when the email is unknown, to even out login timing.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


@router.post(
    "/signup",
    status_code=201,
    response_model=SignupOut,
    dependencies=[Depends(_signup_limit)],
    description=f"Create a household and its owner; sends a confirmation email. {DISCLAIMER}",
)
async def signup(
    body: SignupIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    sender: Annotated[EmailSender, Depends(get_email_sender)],
) -> SignupOut:
    exists = await session.scalar(select(Member.id).where(Member.email == body.email))
    if exists is not None:
        raise HTTPException(status_code=409, detail="email already registered")
    household = Household(name=body.household_name)
    session.add(household)
    await session.flush()
    member = Member(
        household_id=household.id,
        email=body.email,
        password_hash=hash_password(body.password),
        role="owner",
    )
    session.add(member)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="email already registered") from None
    token = new_token("cf_")
    session.add(
        EmailToken(
            member_id=member.id,
            purpose="confirm",
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + CONFIRM_TTL,
        )
    )
    await session.commit()
    link = f"{get_settings().public_base_url}/confirm?token={token}"
    await sender.send(
        body.email,
        "Confirm your Home Awareness email",
        f"Confirm your email (valid 24 hours):\n{link}\n\n{DISCLAIMER}",
    )
    return SignupOut(member_id=member.id, household_id=household.id)


@router.post(
    "/confirm",
    status_code=204,
    description=f"Confirm an email address with the emailed token. {DISCLAIMER}",
)
async def confirm(body: ConfirmIn, session: Annotated[AsyncSession, Depends(get_session)]):
    row = await session.scalar(
        select(EmailToken).where(
            EmailToken.token_hash == hash_token(body.token), EmailToken.purpose == "confirm"
        )
    )
    now = datetime.now(UTC)
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    row.used_at = now
    member = await session.get(Member, row.member_id)
    member.email_confirmed = True
    await session.commit()
    return Response(status_code=204)


@router.post(
    "/login",
    response_model=TokenOut,
    dependencies=[Depends(_login_limit)],
    description=f"Exchange email and password for a bearer token. {DISCLAIMER}",
)
async def login(body: LoginIn, session: Annotated[AsyncSession, Depends(get_session)]) -> TokenOut:
    member = await session.scalar(select(Member).where(Member.email == body.email))
    ok = verify_password(body.password, member.password_hash if member else _DUMMY_HASH)
    if member is None or not ok:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not member.email_confirmed:
        raise HTTPException(status_code=403, detail="email not confirmed")
    return TokenOut(access_token=create_access_token(member.id, member.household_id))


@router.get("/me", response_model=MeOut, description=f"Current member. {DISCLAIMER}")
async def me(member: Annotated[Member, Depends(current_member)]) -> MeOut:
    return MeOut(
        id=member.id, email=member.email, role=member.role, household_id=member.household_id
    )
