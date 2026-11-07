from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.events.models import Event  # noqa: F401  (registers events table)
from app.households.models import Household  # noqa: F401  (registers households table)


class Snapshot(Base):
    """One encrypted JPEG per alerting event. The bytes live on disk (SNAPSHOT_DIR/<path>);
    this row only records where, how big, and when."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)  # of the plain JPEG
    # Relative to SNAPSHOT_DIR: "<household_id>/<uuid>.bin". Never contains a token.
    path: Mapped[str] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
