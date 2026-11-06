from datetime import datetime

from pydantic import BaseModel, field_validator

from app.households.timezone import validate_timezone


class MemberOut(BaseModel):
    id: int
    email: str
    role: str


class HouseholdOut(BaseModel):
    id: int
    name: str
    timezone: str
    members: list[MemberOut]


class InviteOut(BaseModel):
    invite_url: str
    expires_at: datetime


class HouseholdPatch(BaseModel):
    timezone: str

    _tz = field_validator("timezone")(validate_timezone)
