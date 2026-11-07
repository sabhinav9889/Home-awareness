from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PairingCodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["laptop", "phone", "pi", "camera", "other"]
    capabilities: Literal["audio", "video", "audio,video"] = "audio"
    room_id: int | None = None
    # The owner or member confirms everyone on camera knows; required for `video`.
    consent_confirmed: bool = False

    @model_validator(mode="after")
    def _video_needs_consent(self) -> "PairingCodeIn":
        if "video" in self.capabilities and not self.consent_confirmed:
            raise ValueError("confirm everyone on camera knows")
        return self


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
    room_id: int | None = None
    room_name: str | None = None
    capabilities: str = "audio"
    send_snapshots: bool = True
    consent_confirmed_at: datetime | None = None


class DevicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    room_id: int | None = None
    send_snapshots: bool | None = None
    consent_confirmed: Literal[True] | None = None


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
    # True while the household is Away or inside its night hours: cameras then treat a
    # person in view as an alert (`person_away`).
    guarding: bool = False
