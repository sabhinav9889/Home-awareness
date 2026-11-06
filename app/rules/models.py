from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.models import Member  # noqa: F401  (registers members table)
from app.db import Base


class SoundSetting(Base):
    """A member's override of a sound's defaults; absent row means defaults apply."""

    __tablename__ = "sound_settings"
    __table_args__ = (
        UniqueConstraint("member_id", "sound_key"),
        CheckConstraint("repeat_count BETWEEN 1 AND 20"),
        CheckConstraint("window_s BETWEEN 5 AND 3600"),
        CheckConstraint("cooldown_s BETWEEN 0 AND 86400"),
        CheckConstraint(
            "min_confidence BETWEEN 0.05 AND 0.95", name="ck_sound_settings_min_confidence"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"))
    sound_key: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean)
    repeat_count: Mapped[int]
    window_s: Mapped[int]
    cooldown_s: Mapped[int]
    # Null means the catalog's default_min_confidence applies.
    min_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class AlertPreference(Base):
    __tablename__ = "alert_preferences"

    member_id: Mapped[int] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), primary_key=True
    )
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    popup: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    alarm: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    telegram: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ntfy_topic: Mapped[str | None] = mapped_column(String(64), nullable=True)
