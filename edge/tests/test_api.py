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
    _Pending,
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
    assert json.loads(route.calls[0].request.content) == {"mic_clipping": False}


@respx.mock
async def test_heartbeat_reports_mic_clipping(client):
    route = respx.post(f"{BASE}/device/heartbeat").respond(
        200, json={"enabled_sounds": [], "heartbeat_interval_s": 60}
    )
    await client.heartbeat(mic_clipping=True)
    assert json.loads(route.calls[0].request.content) == {"mic_clipping": True}


@respx.mock
async def test_heartbeat_parses_thresholds(client):
    respx.post(f"{BASE}/device/heartbeat").respond(
        200,
        json={
            "enabled_sounds": ["siren"],
            "heartbeat_interval_s": 60,
            "thresholds": {"siren": 0.5},
        },
    )
    cfg = await client.heartbeat()
    assert cfg.thresholds == {"siren": 0.5}


@respx.mock
async def test_heartbeat_from_old_server_without_thresholds(client):
    respx.post(f"{BASE}/device/heartbeat").respond(
        200, json={"enabled_sounds": ["siren"], "heartbeat_interval_s": 60}
    )
    cfg = await client.heartbeat()
    assert cfg.thresholds == {}


@respx.mock
async def test_heartbeat_bad_threshold_value_drops_only_that_key(client):
    respx.post(f"{BASE}/device/heartbeat").respond(
        200,
        json={
            "enabled_sounds": ["siren", "crying"],
            "heartbeat_interval_s": 60,
            "thresholds": {"siren": "loud", "crying": 0.5, "dog_barking": None},
        },
    )
    cfg = await client.heartbeat()
    assert cfg.enabled_sounds == frozenset({"siren", "crying"})
    assert dict(cfg.thresholds) == {"crying": 0.5}


def test_device_config_thresholds_are_read_only():
    src = {"siren": 0.5}
    cfg = DeviceConfig(frozenset({"siren"}), 60, src)
    with pytest.raises(TypeError):
        cfg.thresholds["siren"] = 0.1  # type: ignore[index]
    src["siren"] = 0.1  # the caller's dict was copied
    assert cfg.thresholds["siren"] == 0.5
    assert DeviceConfig(frozenset(), 60).thresholds == {}


@respx.mock
async def test_heartbeat_malformed_thresholds_raises_server_unavailable(client):
    respx.post(f"{BASE}/device/heartbeat").respond(
        200, json={"enabled_sounds": [], "heartbeat_interval_s": 60, "thresholds": ["x"]}
    )
    with pytest.raises(ServerUnavailable):
        await client.heartbeat()


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


NAIVE = datetime(2026, 11, 7, 12, 0, 0)


def test_queue_put_rejects_naive_datetime():
    with pytest.raises(ValueError):
        EventQueue().put("a", 0.5, NAIVE)


@respx.mock
async def test_send_event_rejects_naive_datetime(client):
    with pytest.raises(ValueError):
        await client.send_event("a", 0.5, NAIVE)
    assert not respx.calls


async def test_drain_drops_naive_item_defensively():
    q = EventQueue()
    q._items.append(_Pending("a", 0.5, NAIVE))

    class Fake:
        async def send_event(self, *a):
            raise AssertionError("must not send")

    assert await q.drain(Fake(), NOW) == 0
    assert len(q) == 0


async def test_drain_does_not_discard_other_item_on_concurrent_put():
    q = EventQueue(max_items=2)
    q.put("a", 0.5, NOW)
    q.put("b", 0.5, NOW)
    sent = []

    class Fake:
        async def send_event(self, sound, conf, at):
            sent.append(sound)
            if sound == "a":
                q.put("c", 0.5, NOW)  # evicts "a" at the cap while it is in flight
            return EventResult(1, False, 0)

    assert await q.drain(Fake(), NOW) == 3
    assert sent == ["a", "b", "c"]


@respx.mock
async def test_heartbeat_malformed_body_raises_server_unavailable(client):
    respx.post(f"{BASE}/device/heartbeat").respond(200, json={"nope": 1})
    with pytest.raises(ServerUnavailable):
        await client.heartbeat()


@respx.mock
async def test_pair_malformed_body_raises_server_unavailable():
    respx.post(f"{BASE}/device/pair").respond(201, content=b"not json")
    with pytest.raises(ServerUnavailable):
        await ServerClient(BASE).pair("x")


@respx.mock
async def test_send_event_202_with_bad_body_counts_as_delivered(client):
    respx.post(f"{BASE}/device/events").respond(202, content=b"garbage")
    assert (await client.send_event("a", 0.5, NOW)).event_id == -1
    q = EventQueue()
    q.put("a", 0.5, NOW)
    assert await q.drain(client, NOW) == 1
    assert len(q) == 0


async def test_retain_keeps_only_matching_items_in_order():
    q = EventQueue()
    for s in ("siren", "doorbell", "siren", "scream"):
        q.put(s, 0.5, NOW)
    q.retain(lambda sound: sound != "siren")
    sent = []

    class Fake:
        async def send_event(self, sound, conf, at):
            sent.append(sound)
            return EventResult(1, False, 0)

    assert await q.drain(Fake(), NOW) == 2
    assert sent == ["doorbell", "scream"]


async def test_drain_skips_disallowed_sounds():
    q = EventQueue()
    q.put("siren", 0.5, NOW)
    q.put("doorbell", 0.5, NOW)
    sent = []

    class Fake:
        async def send_event(self, sound, conf, at):
            sent.append(sound)
            return EventResult(1, False, 0)

    assert await q.drain(Fake(), NOW, allow=lambda s: s == "doorbell") == 1
    assert sent == ["doorbell"] and len(q) == 0


# --- final review: rejected events never vanish silently ---------------------------------

SKEW_422 = {
    "detail": [
        {
            "type": "value_error",
            "loc": ["body", "occurred_at"],
            "msg": "Value error, occurred_at is more than 10 minutes from server time",
            "input": "2026-11-07T11:00:00+00:00",
        }
    ]
}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


@respx.mock
async def test_rejected_event_is_logged_once_per_detail_per_10_min(client, caplog):
    caplog.set_level(logging.INFO)
    respx.post(f"{BASE}/device/events").respond(422, json={"detail": "unknown sound type"})
    clock = Clock()
    q = EventQueue(clock=clock)
    q.put("doorbell", 0.5, NOW)
    q.put("knock", 0.5, NOW)
    await q.drain(client, NOW)
    assert _warnings(caplog) == ["server rejected a doorbell event: unknown sound type"]
    clock.t = 599.0
    q.put("doorbell", 0.5, NOW)
    await q.drain(client, NOW)
    assert len(_warnings(caplog)) == 1  # same detail within 10 min: not repeated
    clock.t = 600.0
    q.put("knock", 0.5, NOW)
    await q.drain(client, NOW)
    assert _warnings(caplog)[1:] == ["server rejected a knock event: unknown sound type"]


@respx.mock
async def test_clock_skew_rejection_says_fix_the_system_time(client, caplog):
    caplog.set_level(logging.INFO)
    respx.post(f"{BASE}/device/events").respond(422, json=SKEW_422)
    q = EventQueue(clock=Clock())
    q.put("doorbell", 0.5, NOW)
    q.put("doorbell", 0.5, NOW + timedelta(seconds=1))  # different input, same problem
    await q.drain(client, NOW)
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "This laptop's clock is more than 10 minutes off — fix the system time" in warnings[0]
    assert "doorbell" in warnings[0]


@respx.mock
async def test_404_on_send_points_at_the_server_url(client, caplog):
    caplog.set_level(logging.INFO)
    respx.post(f"{BASE}/device/events").respond(404, json={"detail": "Not Found"})
    q = EventQueue(clock=Clock())
    q.put("doorbell", 0.5, NOW)
    await q.drain(client, NOW)
    (msg,) = _warnings(caplog)
    assert "404" in msg and BASE in msg and "server URL" in msg


@respx.mock
async def test_rejected_carries_status_and_readable_detail(client):
    respx.post(f"{BASE}/device/events").respond(422, json=SKEW_422)
    with pytest.raises(Rejected) as e:
        await client.send_event("doorbell", 0.5, NOW)
    assert e.value.status == 422
    assert e.value.detail == "Value error, occurred_at is more than 10 minutes from server time"
