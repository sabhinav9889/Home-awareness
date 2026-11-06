"""Job runner seam; Tasks 8/9 fill this in."""

from typing import Protocol


class JobRunner(Protocol):
    async def enqueue_alert(self, alert_id: int) -> None: ...


class _NoopJobRunner:
    async def enqueue_alert(self, alert_id: int) -> None:
        return None


def get_job_runner() -> JobRunner:
    return _NoopJobRunner()


class RecordingJobRunner:
    """Test double: records the alert ids it was asked to enqueue."""

    def __init__(self) -> None:
        self.enqueued: list[int] = []

    async def enqueue_alert(self, alert_id: int) -> None:
        self.enqueued.append(alert_id)
