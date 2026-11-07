from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.households.timezone import validate_timezone


class MemberOut(BaseModel):
    id: int
    email: str
    role: str


class HouseholdOut(BaseModel):
    id: int
    name: str
    timezone: str
    away: bool = False
    night_start: time | None = None
    night_end: time | None = None
    snapshot_retention_days: Literal[1, 7, 30] = 7
    members: list[MemberOut]


class InviteOut(BaseModel):
    invite_url: str
    expires_at: datetime


class HouseholdPatch(BaseModel):
    """`away` may be set by any member; every other field is owner-only."""

    model_config = ConfigDict(extra="forbid")
    timezone: str | None = None
    away: bool | None = None
    night_start: time | None = None
    night_end: time | None = None
    snapshot_retention_days: Literal[1, 7, 30] | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else validate_timezone(v)

    @model_validator(mode="after")
    def _night_hours(self) -> "HouseholdPatch":
        sent = self.model_fields_set
        if ("night_start" in sent or "night_end" in sent) and (
            (self.night_start is None) != (self.night_end is None)
        ):
            raise ValueError("night_start and night_end must be set or cleared together")
        if self.night_start is not None and self.night_start == self.night_end:
            raise ValueError("night_start and night_end must differ")
        return self

    @property
    def owner_fields(self) -> set[str]:
        return {
            f
            for f in self.model_fields_set
            if f != "away"
            and not (f in ("timezone", "snapshot_retention_days") and getattr(self, f) is None)
        }
