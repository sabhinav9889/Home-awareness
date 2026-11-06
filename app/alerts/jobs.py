"""Job runner seam: enqueue alert delivery on Celery or in-process."""

import asyncio
import logging
from typing import Protocol

from app.alerts.delivery import MAX_ALERT_AGE, deliver_alert
from app.config import get_settings

logger = logging.getLogger(__name__)


class JobRunner(Protocol):
    async def enqueue_alert(self, alert_id: int) -> None: ...


class CeleryJobRunner:
    async def enqueue_alert(self, alert_id: int) -> None:
        from app.alerts.celery_app import deliver_alert_task

        # apply_async blocks (kombu retries a dead broker): run it off the event loop and
        # fail fast so enqueue_or_fallback can deliver in-process instead.
        # expires: a message still queued after 10 minutes is discarded (see MAX_ALERT_AGE).
        await asyncio.to_thread(
            deliver_alert_task.apply_async,
            (alert_id,),
            retry=False,
            expires=int(MAX_ALERT_AGE.total_seconds()),
        )


class InProcessJobRunner:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def enqueue_alert(self, alert_id: int) -> None:
        task = asyncio.create_task(deliver_alert(alert_id))
        self._tasks.add(task)  # keep a ref so the task isn't garbage collected mid-flight
        task.add_done_callback(lambda t: self._done(alert_id, t))

    def _done(self, alert_id: int, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("alert delivery failed for alert %s", alert_id, exc_info=exc)


_inprocess = InProcessJobRunner()


def get_job_runner() -> JobRunner:
    return _inprocess if get_settings().job_runner == "inprocess" else CeleryJobRunner()


async def enqueue_or_fallback(jobs: JobRunner, alert_id: int) -> None:
    """Enqueue a committed alert; never let a queue failure drop it. On any error deliver
    in this process (deliver_alert's atomic pending->delivering claim prevents a double send)."""
    try:
        await jobs.enqueue_alert(alert_id)
    except Exception:
        logger.error("enqueue failed for alert %s; delivering in-process", alert_id, exc_info=True)
        await _inprocess.enqueue_alert(alert_id)


class RecordingJobRunner:
    """Test double: records the alert ids it was asked to enqueue."""

    def __init__(self) -> None:
        self.enqueued: list[int] = []

    async def enqueue_alert(self, alert_id: int) -> None:
        self.enqueued.append(alert_id)
