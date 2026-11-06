from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, desc
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.devices.models import Device  # noqa: F401  (registers devices table)
from app.households.models import Household  # noqa: F401  (registers households table)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_household_received", "household_id", desc("received_at")),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"))
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sound_key: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    merged: Mapped[bool] = mapped_column(default=False, server_default="false")
