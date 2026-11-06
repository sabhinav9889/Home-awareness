import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ha_edge.api import (
    DeviceConfig,
    DeviceRevoked,
    EventQueue,
    EventResult,
    PairingFailed,
    RateLimited,
    Rejected,
    ServerClient,
    ServerUnavailable,
)

BASE = "http://srv.test"
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
OK = {"event_id": 1, "merged": False, "alerts": 0}


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield ServerClient(BASE, "secret-key-123", http=http)


@respx.mock
async def test_pair_returns_id_and_key():
    respx.post(f"{BASE}/device/pair").respond(201, json={"device_id": 7, "device_key": "k"})
    c = ServerClient(BASE)
    assert await c.pair("ABC123") == (7, "k")
    assert json.loads(respx.calls[0].request.content) == {"code": "ABC123"}


@respx.mock
async def test_pair_invalid_code_raises_pairing_failed():
    respx.post(f"{BASE}/device/pair").respond(
        400, json={"detail": "invalid or expired pairing code"}
    )
    with pytest.raises(PairingFailed):
        await ServerClient(BASE).pair("bad")


@respx.mock
async def test_heartbeat_parses_config(client):
    route = respx.post(f"{BASE}/device/heartbeat").respond(
        200, json={"enabled_sounds": ["doorbell", "smoke_alarm"], "heartbeat_interval_s": 60}
    )
    cfg = await client.heartbeat()
    assert cfg == DeviceConfig(frozenset({"doorbell", "smoke_alarm"}), 60)
    assert route.calls[0].request.headers["X-Device-Key"] == "secret-key-123"


@respx.mock
async def test_send_event_posts_exact_fields_only(client):
    route = respx.post(f"{BASE}/device/events").respond(
        202, json={"event_id": 5, "merged": True, "alerts": 2}
    )
    res = await client.send_event("doorbell", 0.8, NOW)
    assert res == EventResult(5, True, 2)
    body = json.loads(route.calls[0].request.content)
    assert set(body) == {"type", "confidence", "occurred_at"}
    assert body["type"] == "doorbell"
    parsed = datetime.fromisoformat(body["occurred_at"])
    assert parsed.utcoffset() is not None
    assert parsed == NOW


@respx.mock
async def test_401_raises_device_revoked(client):
    respx.post(f"{BASE}/device/heartbeat").respond(401, json={"detail": "x"})
    with pytest.raises(DeviceRevoked):
        await client.heartbeat()


@respx.mock
async def test_5xx_and_connect_error_raise_server_unavailable(client):
    route = respx.post(f"{BASE}/device/events")
    route.respond(503)
    with pytest.raises(ServerUnavailable):
        await client.send_event("doorbell", 0.5, NOW)
    route.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ServerUnavailable):
        await client.send_event("doorbell", 0.5, NOW)


@respx.mock
async def test_429_uses_retry_after_header(client):
    respx.post(f"{BASE}/device/events").respond(429, headers={"Retry-After": "7"})
    with pytest.raises(RateLimited) as e:
        await client.send_event("doorbell", 0.5, NOW)
    assert e.value.retry_after_s == 7.0


@respx.mock
async def test_429_defaults_when_header_missing_or_bad(client):
    route = respx.post(f"{BASE}/device/events")
    route.respond(429)
    with pytest.raises(RateLimited) as e:
        await client.send_event("doorbell", 0.5, NOW)
    assert e.value.retry_after_s == 1.0
    route.respond(429, headers={"Retry-After": "soon"})
    with pytest.raises(RateLimited) as e:
        await client.send_event("doorbell", 0.5, NOW)
    assert e.value.retry_after_s == 1.0


@respx.mock
async def test_422_raises_rejected(client):
    respx.post(f"{BASE}/device/events").respond(422, json={"detail": "skew"})
    with pytest.raises(Rejected):
        await client.send_event("doorbell", 0.5, NOW)


@respx.mock
async def test_device_key_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(f"{BASE}/device/heartbeat").respond(
        200, json={"enabled_sounds": [], "heartbeat_interval_s": 60}
    )
    await client.heartbeat()
    assert "secret-key-123" not in caplog.text


@respx.mock
async def test_queue_keeps_items_on_unavailable_and_drops_after_10_min(client):
    route = respx.post(f"{BASE}/device/events")
    route.mock(side_effect=httpx.ConnectError("down"))
    q = EventQueue()
    q.put("doorbell", 0.5, NOW - timedelta(minutes=11))
    q.put("knock", 0.6, NOW - timedelta(seconds=30))
    q.put("dog_bark", 0.7, NOW - timedelta(seconds=10))
    with pytest.raises(ServerUnavailable):
        await q.drain(client, NOW)
    assert len(q) == 2  # stale dropped, rest kept
    route.mock(side_effect=None)
    route.respond(202, json=OK)
    assert await q.drain(client, NOW) == 2
    assert len(q) == 0
    sent = [
        json.loads(c.request.content)["type"]
        for c in route.calls
        if c.optional_response is not None
    ]
    assert sent == ["knock", "dog_bark"]


@respx.mock
async def test_queue_drops_rejected_and_continues(client):
    route = respx.post(f"{BASE}/device/events")
    route.side_effect = [httpx.Response(422), httpx.Response(202, json=OK)]
    q = EventQueue()
    q.put("a", 0.5, NOW)
    q.put("b", 0.5, NOW)
    assert await q.drain(client, NOW) == 1
    assert len(q) == 0


@respx.mock
async def test_queue_reraises_revoked(client):
    respx.post(f"{BASE}/device/events").respond(401)
    q = EventQueue()
    q.put("a", 0.5, NOW)
    with pytest.raises(DeviceRevoked):
        await q.drain(client, NOW)


@respx.mock
async def test_queue_keeps_item_and_stops_on_rate_limited(client):
    respx.post(f"{BASE}/device/events").respond(429, headers={"Retry-After": "3"})
    q = EventQueue()
    q.put("a", 0.5, NOW)
    q.put("b", 0.5, NOW)
    with pytest.raises(RateLimited) as e:
        await q.drain(client, NOW)
    assert e.value.retry_after_s == 3.0
    assert len(q) == 2


async def test_queue_cap_drops_oldest_and_logs_once(caplog):
    caplog.set_level(logging.WARNING)
    q = EventQueue()
    for i in range(505):
        q.put("a", 0.5, NOW + timedelta(seconds=i))
    assert len(q) == 500
    assert q.oldest_occurred_at() == NOW + timedelta(seconds=5)
    assert len([r for r in caplog.records if "queue full" in r.getMessage()]) == 1
