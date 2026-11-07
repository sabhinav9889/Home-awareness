"""Stale-alert sweep: an alert stuck pending/delivering (worker crash, lost message) must
not look in-flight forever, so fail it visibly. Delivery is at-most-once: never re-sent."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.models import Alert, Delivery
from app.metrics import ALERTS_STALE

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=15)
SWEEP_INTERVAL_S = 300


async def sweep_stale_alerts(session: AsyncSession, now: datetime) -> int:
    """Fail alerts older than STALE_AFTER that never finished, and their pending deliveries.
    Cross-household sweep (sanctioned Tenant bypass). One transaction; returns the count.

    Race, intended: a worker that is still delivering (very slow, not dead) may finish after
    this sweep and overwrite failed -> sent/partial on the alert and its deliveries. That is
    the truth (the message did go out), and nothing is ever re-sent either way, so
    at-most-once holds; the sweep only exists so a dead worker can't leave an alert looking
    in-flight forever."""
    ids = (
        await session.scalars(
            update(Alert)
            .where(
                Alert.status.in_(("pending", "delivering")),
                Alert.created_at < now - STALE_AFTER,
            )
            .values(status="failed")
            .returning(Alert.id)
        )
    ).all()
    if ids:
        await session.execute(
            update(Delivery)
            .where(Delivery.alert_id.in_(ids), Delivery.status == "pending")
            .values(status="failed", last_error="stale")
        )
    await session.commit()
    if ids:
        ALERTS_STALE.inc(len(ids))
        log.warning("failed %d stale alert(s) that never finished delivering", len(ids))
    return len(ids)


async def sweep_loop(
    session_factory: Callable[[], Any],
    *,
    interval_s: float = SWEEP_INTERVAL_S,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """In-process scheduler. Logs and continues on any error; stops only when cancelled."""
    clock = clock or (lambda: datetime.now(UTC))
    while True:
        try:
            async with session_factory() as session:
                await sweep_stale_alerts(session, clock())
        except Exception:  # noqa: BLE001
            log.exception("stale alert sweep failed; will retry")
        await asyncio.sleep(interval_s)
