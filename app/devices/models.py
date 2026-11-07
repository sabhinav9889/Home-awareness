from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, false, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.households.models import Household  # noqa: F401  (registers households table)
from app.rooms.models import Room  # noqa: F401  (registers rooms table)

PAIRING_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8
PAIRING_CODE_TTL_MINUTES = 10


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("kind IN ('laptop','phone','pi','camera','other')"),
        CheckConstraint("status IN ('pending','online','offline')"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    # Reported by the sensor's heartbeat: its microphone input level is high enough to clip.
    mic_clipping: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    room_id: Mapped[int | None] = mapped_column(
        ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True
    )
    # "audio", "video" or "audio,video".
    capabilities: Mapped[str] = mapped_column(String(16), default="audio", server_default="audio")
    # Whether an alerting event may carry a snapshot (the owner's call).
    send_snapshots: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    consent_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consent_confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL"), nullable=True
    )


class PairingCode(Base):
    __tablename__ = "pairing_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    room_id: Mapped[int | None] = mapped_column(
        ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True
    )
    capabilities: Mapped[str] = mapped_column(String(16), default="audio", server_default="audio")
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
