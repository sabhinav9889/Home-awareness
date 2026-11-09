from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.catalog import INCIDENT_KEYS, SOUNDS

MAX_SKEW = timedelta(minutes=10)


class EventIn(BaseModel):
    """Only these fields are accepted: raw audio must never reach the server."""

    model_config = ConfigDict(extra="forbid")

    type: str
    confidence: float = Field(ge=0, le=1)
    occurred_at: datetime

    @field_validator("type")
    @classmethod
    def _known_sound(cls, v: str) -> str:
        if v not in SOUNDS or v in INCIDENT_KEYS:  # incidents are server-raised only
            raise ValueError("unknown sound type")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _aware_and_recent(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        if abs(v - datetime.now(UTC)) > MAX_SKEW:
            raise ValueError("occurred_at is more than 10 minutes from server time")
        return v


MERGED_DOC = (
    "True when this report was folded into another sensor's report of the same sound in the "
    "same household moments earlier. A merged report may still alert: members whose "
    "stricter (higher) minimum confidence the earlier report missed are alerted if this "
    "report crosses it."
)


class EventAccepted(BaseModel):
    event_id: int
    merged: bool = Field(description=MERGED_DOC)
    alerts: int


class EventOut(BaseModel):
    id: int
    sound_key: str
    label: str
    device_name: str | None
    room_name: str | None
    confidence: float
    received_at: datetime
    merged: bool = Field(description=MERGED_DOC)
    has_snapshot: bool = False
