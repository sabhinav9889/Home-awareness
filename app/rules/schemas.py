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


class SoundSettingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    repeat_count: int = Field(ge=1, le=20)
    window_s: int = Field(ge=5, le=3600)
    cooldown_s: int = Field(ge=0, le=86400)


class SoundSettingOut(BaseModel):
    sound_key: str
    enabled: bool
    repeat_count: int
    window_s: int
    cooldown_s: int


class AlertPreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    popup: bool
    alarm: bool
    telegram: bool
    ntfy_topic: NtfyTopic | None = None


class AlertPreferencesOut(BaseModel):
    popup: bool
    alarm: bool
    telegram: bool
    telegram_linked: bool
    ntfy_topic: str | None


class NtfyTopicOut(BaseModel):
    ntfy_topic: str
