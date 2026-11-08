class RecordingJobRunner:
    """Test double: records the alert ids it was asked to enqueue."""

    def __init__(self) -> None:
        self.enqueued: list[int] = []

    async def enqueue_alert(self, alert_id: int) -> None:
        self.enqueued.append(alert_id)


def make_jpeg(size: int = 2000) -> bytes:
    """A minimal-looking JPEG: SOI + APP0 marker, filler, EOI. Enough for the server's check."""
    return b"\xff\xd8\xff\xe0" + b"\x00" * (size - 6) + b"\xff\xd9"
