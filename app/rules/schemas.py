from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.catalog import Reliability

NtfyTopic = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{20,64}$")]


class SoundOut(BaseModel):
    key: str
    label: str
    group: str
    default_enabled: bool
    reliability: Reliability
    default_repeat: int
    default_window_s: int
    default_cooldown_s: int
    default_min_confidence: float


class SoundSettingIn(BaseModel):
    """A member's override of one sound.

    `min_confidence` (0.05-0.95) is optional and tri-state: omit it to keep any existing
    override unchanged, send a number to set it, or send null to clear it back to the
    catalog's `default_min_confidence`.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool
    repeat_count: int = Field(ge=1, le=20)
    window_s: int = Field(ge=5, le=3600)
    cooldown_s: int = Field(ge=0, le=86400)
    min_confidence: float | None = Field(default=None, ge=0.05, le=0.95)


class SoundSettingOut(BaseModel):
    sound_key: str
    enabled: bool
    repeat_count: int
    window_s: int
    cooldown_s: int
    min_confidence: float  # effective: the member's override or the catalog default


class AlertPreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    popup: bool
    alarm: bool
    telegram: bool
    # Required, but may be null: omitting it is a 422, so a client that forgets the field
    # can't silently switch off someone's ntfy alerts. Send null to clear it.
    ntfy_topic: NtfyTopic | None


class AlertPreferencesOut(BaseModel):
    popup: bool
    alarm: bool
    telegram: bool
    telegram_linked: bool
    ntfy_topic: str | None


class NtfyTopicOut(BaseModel):
    ntfy_topic: str
