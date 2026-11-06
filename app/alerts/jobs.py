"""Job runner seam: enqueue alert delivery on Celery or in-process."""

import asyncio
from typing import Protocol

from app.alerts.delivery import deliver_alert
from app.config import get_settings


class JobRunner(Protocol):
    async def enqueue_alert(self, alert_id: int) -> None: ...


class CeleryJobRunner:
    async def enqueue_alert(self, alert_id: int) -> None:
        from app.alerts.celery_app import deliver_alert_task

        deliver_alert_task.delay(alert_id)


class InProcessJobRunner:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def enqueue_alert(self, alert_id: int) -> None:
        task = asyncio.create_task(deliver_alert(alert_id))
        self._tasks.add(task)  # keep a ref so the task isn't garbage collected mid-flight
        task.add_done_callback(self._tasks.discard)


_inprocess = InProcessJobRunner()


def get_job_runner() -> JobRunner:
    return _inprocess if get_settings().job_runner == "inprocess" else CeleryJobRunner()


class RecordingJobRunner:
    """Test double: records the alert ids it was asked to enqueue."""

    def __init__(self) -> None:
        self.enqueued: list[int] = []

    async def enqueue_alert(self, alert_id: int) -> None:
        self.enqueued.append(alert_id)
