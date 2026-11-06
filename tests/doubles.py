class RecordingJobRunner:
    """Test double: records the alert ids it was asked to enqueue."""

    def __init__(self) -> None:
        self.enqueued: list[int] = []

    async def enqueue_alert(self, alert_id: int) -> None:
        self.enqueued.append(alert_id)
