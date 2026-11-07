from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PairingCodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["laptop", "phone", "pi", "other"]


class PairingCodeOut(BaseModel):
    code: str
    expires_at: datetime


class PairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=32)


class PairOut(BaseModel):
    device_id: int
    device_key: str


class DeviceOut(BaseModel):
    id: int
    name: str
    kind: str
    status: str
    last_seen_at: datetime | None
    mic_clipping: bool


class HeartbeatIn(BaseModel):
    # Unknown fields are ignored (not forbidden) so a newer sensor keeps working against
    # this server; the body itself is optional for sensors that predate it.
    model_config = ConfigDict(extra="ignore")
    mic_clipping: bool = False


class WhoamiOut(BaseModel):
    device_id: int
    household_id: int


class DeviceConfigOut(BaseModel):
    enabled_sounds: list[str]
    heartbeat_interval_s: int
    # Per enabled sound: the lowest min_confidence any member who enabled it wants.
    thresholds: dict[str, float]
