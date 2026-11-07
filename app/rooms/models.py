from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.households.models import Household  # noqa: F401  (registers households table)

ROOM_KINDS = ("common", "entrance", "bedroom", "bathroom", "outdoor", "other")
# Cameras placed in these rooms start with snapshots off.
PRIVATE_ROOM_KINDS = ("bedroom", "bathroom")


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint("kind IN ('common','entrance','bedroom','bathroom','outdoor','other')"),
        UniqueConstraint("household_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
