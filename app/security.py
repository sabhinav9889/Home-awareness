import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

_hasher = PasswordHasher()
# Verified against when a login email is unknown, to even out timing.
DUMMY_HASH = _hasher.hash("dummy-password-for-timing")
_ALGORITHM = "HS256"


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class TokenClaims:
    member_id: int
    household_id: int


# argon2 is deliberately slow (tens of ms of CPU): run it in a worker thread so one login
# never stalls the event loop that also carries live alerts.
async def hash_password(p: str) -> str:
    return await asyncio.to_thread(_hasher.hash, p)


async def verify_password(p: str, h: str) -> bool:
    return await asyncio.to_thread(_verify, p, h)


def _verify(p: str, h: str) -> bool:
    try:
        return _hasher.verify(h, p)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(member_id: int, household_id: int) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(member_id),
        "hid": household_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(t: str) -> TokenClaims:
    try:
        data = jwt.decode(
            t, get_settings().jwt_secret, algorithms=[_ALGORITHM], options={"require": ["exp"]}
        )
        return TokenClaims(member_id=int(data["sub"]), household_id=int(data["hid"]))
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as e:
        raise InvalidToken from e


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()
