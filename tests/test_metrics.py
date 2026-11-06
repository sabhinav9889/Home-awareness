import json
import logging
import re
from datetime import UTC, datetime

import pytest

from tests.test_devices import _pair

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")


async def _sample(client, line_prefix: str) -> float:
    text = (await client.get("/metrics")).text
    for line in text.splitlines():
        if line.startswith(line_prefix + " "):
            return float(line.split()[-1])
    return 0.0  # counter not created yet


def _event():
    return {"type": "scream", "confidence": 0.9, "occurred_at": datetime.now(UTC).isoformat()}


async def test_metrics_counts_events_and_alerts(client, signed_up):
    _, h = await signed_up()
    paired = await _pair(client, h)
    dk = {"X-Device-Key": paired["device_key"]}
    received = 'ha_events_received_total{sound="scream"}'
    alerts = 'ha_alerts_created_total{sound="scream"}'
    before = (await _sample(client, received), await _sample(client, alerts))
    assert (await client.post("/device/events", json=_event(), headers=dk)).status_code == 202
    r = await client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert await _sample(client, received) == before[0] + 1.0
    assert await _sample(client, alerts) == before[1] + 1.0


async def test_metrics_counts_merged_events(client, signed_up):
    _, h = await signed_up()
    dk = {"X-Device-Key": (await _pair(client, h))["device_key"]}
    other = {"X-Device-Key": (await _pair(client, h, name="Hall"))["device_key"]}
    before = await _sample(client, "ha_events_merged_total")
    await client.post("/device/events", json=_event(), headers=dk)
    r = await client.post("/device/events", json=_event(), headers=other)  # within merge window
    assert r.json()["merged"] is True
    assert await _sample(client, "ha_events_merged_total") == before + 1.0


async def test_request_log_has_route_template_and_no_secrets(client, monkeypatch, caplog):
    from app.alerts import router as alerts_router

    secret = "whsec-SUPERSECRET"
    monkeypatch.setattr(
        alerts_router,
        "get_settings",
        lambda: type("S", (), {"telegram_webhook_secret": secret, "telegram_bot_token": None})(),
    )
    caplog.set_level(logging.INFO)
    await client.post(
        f"/telegram/webhook/{secret}?token=QSECRET",
        json={"x": "BODYSECRET"},
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )
    r = await client.get("/health?x=1&jwt=eyJabc", headers={"Authorization": "Bearer HDRSECRET"})
    assert "x-request-id" in r.headers
    await client.get("/no/such/path/PATHSECRET")
    for leaked in (secret, "QSECRET", "BODYSECRET", "x=1", "eyJabc", "HDRSECRET", "PATHSECRET"):
        assert leaked not in caplog.text
    lines = [
        json.loads(m)
        for m in (rec.getMessage() for rec in caplog.records if rec.name == "app.request")
    ]
    paths = [line["path"] for line in lines]
    assert "/telegram/webhook/{secret}" in paths and "/health" in paths
    assert "unmatched" in paths
    health = next(line for line in lines if line["path"] == "/health")
    assert set(health) == {"method", "path", "status", "duration_ms", "request_id"}
    assert health["method"] == "GET" and isinstance(health["duration_ms"], float)
    assert re.fullmatch(r"[0-9a-f]{32}", health["request_id"])
    assert health["request_id"] == r.headers["x-request-id"]


async def test_drop_all_guard_refuses_non_test_database():
    from tests.conftest import require_test_database

    with pytest.raises(RuntimeError, match="_test"):
        require_test_database("postgresql+asyncpg://home:home@localhost:5432/home")
    require_test_database("postgresql+asyncpg://home:home@localhost:5432/home_test?x=1")
