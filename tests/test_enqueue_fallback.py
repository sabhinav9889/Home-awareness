from datetime import UTC, datetime

import pytest
from sqlalchemy import update

import app.events.router as events_router
from app.alerts import jobs
from app.alerts.models import Alert
from tests.test_devices import _pair


class BrokenRunner:
    def __init__(self):
        self.calls = []

    async def enqueue_alert(self, alert_id):
        self.calls.append(alert_id)
        raise ConnectionError("broker down")


@pytest.fixture
def broken(monkeypatch, session, frozen_ratelimit_clock):
    from app.alerts.jobs import get_job_runner
    from app.main import app

    runner = BrokenRunner()
    app.dependency_overrides[get_job_runner] = lambda: runner
    delivered = []

    async def fake_deliver(alert_id):  # stands in for deliver_alert with mocked channels
        delivered.append(alert_id)
        await session.execute(update(Alert).where(Alert.id == alert_id).values(status="sent"))

    monkeypatch.setattr(jobs, "deliver_alert", fake_deliver)
    return runner, delivered


async def _drain():
    import asyncio

    await asyncio.gather(*list(jobs._inprocess._tasks))


async def test_event_survives_enqueue_failure(client, signed_up, session, broken, monkeypatch):
    runner, delivered = broken
    published = []

    async def fake_publish(redis, household_id, msg):
        published.append(msg)

    monkeypatch.setattr(events_router, "publish_to_household", fake_publish)
    _, h = await signed_up()
    dk = {"X-Device-Key": (await _pair(client, h))["device_key"]}
    body = {"type": "scream", "confidence": 0.9, "occurred_at": datetime.now(UTC).isoformat()}
    r = await client.post("/device/events", json=body, headers=dk)
    assert r.status_code == 202 and r.json()["alerts"] == 1
    await _drain()
    alert_id = runner.calls[0]
    assert delivered == [alert_id]
    assert (await session.get(Alert, alert_id)).status == "sent"
    assert len(published) == 1  # live publish still happened


async def test_test_alert_survives_enqueue_failure(client, signed_up, session, broken):
    runner, delivered = broken
    _, h = await signed_up()
    r = await client.post("/me/test-alert", headers=h)
    assert r.status_code == 202
    await _drain()
    assert delivered == [r.json()["alert_id"]] == runner.calls
