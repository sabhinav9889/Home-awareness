import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_member
from app.auth.models import AgentToken, EmailToken, Member
from app.auth.schemas import (
    ConfirmIn,
    ConfirmResendIn,
    LoginIn,
    MeOut,
    ResetConfirmIn,
    ResetRequestIn,
    SignupIn,
    SignupOut,
    TokenOut,
)
from app.config import get_settings
from app.db import get_session
from app.disclaimer import DISCLAIMER
from app.email import EmailSender, get_email_sender
from app.households.models import Household, Invite
from app.live.hub import publish_disconnect
from app.ratelimit import by_ip, rate_limit
from app.redis import get_redis
from app.rules.models import AlertPreference
from app.security import (
    DUMMY_HASH,
    create_access_token,
    hash_password,
    hash_token,
    new_token,
    verify_password,
)

CONFIRM_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
# Ruling 1b-R4: a reset never rotates the ntfy topic or unlinks Telegram (a safety app must
# never silently stop alerting the real phone), so the member is told to check them.
ALERT_CHANNELS_NOTE = (
    "If someone else had access to your account, regenerate your ntfy topic "
    "and check your Telegram link in Alert channels."
)

router = APIRouter(prefix="/auth", tags=["auth"])
_signup_limit = rate_limit("signup", 10, 60, by_ip)
_login_limit = rate_limit("login", 10, 60, by_ip)
_reset_limit = rate_limit("password-reset", 10, 60, by_ip)
_resend_limit = rate_limit("confirm-resend", 10, 60, by_ip)
log = logging.getLogger(__name__)


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
    if body.invite_token is not None:
        # Atomic single-use claim: only one concurrent signup can win the invite.
        household_id = await session.scalar(
            update(Invite)
            .where(
                Invite.token_hash == hash_token(body.invite_token),
                Invite.used_at.is_(None),
                Invite.expires_at > datetime.now(UTC),
            )
            .values(used_at=datetime.now(UTC))
            .returning(Invite.household_id)
        )
        if household_id is None:
            raise HTTPException(status_code=400, detail="invalid or expired invite")
        role = "member"
    else:
        household = Household(name=body.household_name, timezone=body.timezone)
        session.add(household)
        await session.flush()
        household_id = household.id
        role = "owner"
    member = Member(
        household_id=household_id,
        email=body.email,
        password_hash=await hash_password(body.password),
        role=role,
    )
    session.add(member)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="email already registered") from None
    session.add(AlertPreference(member_id=member.id, household_id=household_id))
    token = _new_confirm_token(session, member.id)
    await session.commit()
    # The account exists now: a mail failure must not turn into a 500 (the user would retry
    # and hit 409). They can ask for a fresh link via /auth/confirm/resend.
    await _send_confirmation(sender, member.id, body.email, token)
    return SignupOut(member_id=member.id, household_id=household_id)


async def _send_confirmation(sender: EmailSender, member_id: int, email: str, token: str) -> None:
    link = f"{get_settings().public_base_url}/confirm?token={token}"
    try:
        await sender.send(
            email,
            "Confirm your Home Awareness email",
            f"Confirm your email (valid 24 hours):\n{link}\n\n{DISCLAIMER}",
        )
    except Exception as e:  # noqa: BLE001  logged, never raised: the user can resend
        log.warning(
            "confirmation email for member %s failed: %s: %s", member_id, type(e).__name__, e
        )


def _new_confirm_token(session: AsyncSession, member_id: int) -> str:
    token = new_token("cf_")
    session.add(
        EmailToken(
            member_id=member_id,
            purpose="confirm",
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + CONFIRM_TTL,
        )
    )
    return token


@router.post(
    "/confirm/resend",
    status_code=202,
    dependencies=[Depends(_resend_limit)],
    description=(
        "Email a fresh confirmation link if the address is registered and not yet confirmed; "
        f"earlier links stop working. {DISCLAIMER}"
    ),
)
async def confirm_resend(
    body: ConfirmResendIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    sender: Annotated[EmailSender, Depends(get_email_sender)],
) -> dict[str, str]:
    # Same response whether the address is unknown, confirmed or pending: no enumeration.
    member = await session.scalar(select(Member).where(Member.email == body.email))
    if member is not None and not member.email_confirmed:
        await session.execute(
            update(EmailToken)
            .where(
                EmailToken.member_id == member.id,
                EmailToken.purpose == "confirm",
                EmailToken.used_at.is_(None),
            )
            .values(used_at=datetime.now(UTC))
        )
        token = _new_confirm_token(session, member.id)
        await session.commit()
        await _send_confirmation(sender, member.id, member.email, token)
    return {"detail": "if the address awaits confirmation, an email was sent"}


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
    ok = await verify_password(body.password, member.password_hash if member else DUMMY_HASH)
    if member is None or not ok:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not member.email_confirmed:
        raise HTTPException(status_code=403, detail="email not confirmed")
    return TokenOut(
        access_token=create_access_token(member.id, member.household_id, member.token_version)
    )


@router.get("/me", response_model=MeOut, description=f"Current member. {DISCLAIMER}")
async def me(member: Annotated[Member, Depends(current_member)]) -> MeOut:
    return MeOut(
        id=member.id, email=member.email, role=member.role, household_id=member.household_id
    )


@router.post(
    "/password-reset/request",
    status_code=202,
    dependencies=[Depends(_reset_limit)],
    description=f"Email a password reset link if the address is registered. {DISCLAIMER}",
)
async def password_reset_request(
    body: ResetRequestIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    sender: Annotated[EmailSender, Depends(get_email_sender)],
) -> dict[str, str]:
    # Same response and same token work whether or not the address exists.
    token = new_token("rs_")
    token_hash = hash_token(token)
    member = await session.scalar(select(Member).where(Member.email == body.email))
    if member is not None:
        session.add(
            EmailToken(
                member_id=member.id,
                purpose="reset",
                token_hash=token_hash,
                expires_at=datetime.now(UTC) + RESET_TTL,
            )
        )
        await session.commit()
        link = f"{get_settings().public_base_url}/reset-password?token={token}"
        await sender.send(
            member.email,
            "Reset your Home Awareness password",
            f"Reset your password (valid 1 hour):\n{link}\n\n{DISCLAIMER}",
        )
    return {"detail": "if the address is registered, a reset email was sent"}


@router.post(
    "/password-reset/confirm",
    status_code=200,
    dependencies=[Depends(_reset_limit)],
    description=(
        "Set a new password using the emailed reset token. Signs out every session but keeps "
        "your alert channels: `check_alert_channels` is always true, as a reminder to "
        f"regenerate the ntfy topic and check the Telegram link. {DISCLAIMER}"
    ),
)
async def password_reset_confirm(
    body: ResetConfirmIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    sender: Annotated[EmailSender, Depends(get_email_sender)],
) -> dict[str, bool]:
    now = datetime.now(UTC)
    member_id = await session.scalar(
        update(EmailToken)
        .where(
            EmailToken.token_hash == hash_token(body.token),
            EmailToken.purpose == "reset",
            EmailToken.used_at.is_(None),
            EmailToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(EmailToken.member_id)
    )
    if member_id is None:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    member = await session.get(Member, member_id)
    email = member.email
    member.password_hash = await hash_password(body.new_password)
    # Sign out every session in the same transaction: JWTs die with the version bump,
    # agent tokens are revoked.
    await session.execute(
        update(Member).where(Member.id == member_id).values(token_version=Member.token_version + 1)
    )
    await session.execute(
        update(AgentToken)
        .where(AgentToken.member_id == member_id, AgentToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    # A stolen session may have invited an attacker: drop this member's pending invites.
    await session.execute(
        delete(Invite).where(
            Invite.created_by == member_id, Invite.used_at.is_(None), Invite.expires_at > now
        )
    )
    # Retire any other outstanding reset links for this member.
    await session.execute(
        update(EmailToken)
        .where(
            EmailToken.member_id == member_id,
            EmailToken.purpose == "reset",
            EmailToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    await session.commit()
    try:  # best effort: close open sockets now; a missed one is refused at its next check
        await publish_disconnect(redis, [member_id])
    except Exception as e:  # noqa: BLE001
        log.warning("reset disconnect for member %s failed: %s", member_id, type(e).__name__)
    try:  # best effort: the reset already happened; a lost notice must not undo or fail it
        await sender.send(
            email,
            "Your Home Awareness password was changed",
            "Your Home Awareness password was changed and every session was signed out.\n\n"
            f"{ALERT_CHANNELS_NOTE}\n\n{DISCLAIMER}",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("reset notice for member %s failed: %s", member_id, type(e).__name__)
    return {"check_alert_channels": True}
