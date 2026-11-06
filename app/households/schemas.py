from datetime import datetime

from pydantic import BaseModel


class MemberOut(BaseModel):
    id: int
    email: str
    role: str


class HouseholdOut(BaseModel):
    id: int
    name: str
    members: list[MemberOut]


class InviteOut(BaseModel):
    invite_url: str
    expires_at: datetime
