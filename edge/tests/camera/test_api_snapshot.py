"""Snapshots through the server client and the event queue (no camera, no disk)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ha_edge.api import (
    DeviceConfig,
    DeviceRevoked,
    EventQueue,
    RateLimited,
    Rejected,
    ServerClient,
    ServerUnavailable,
)

BASE = "http://srv.test"
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
OK = {"event_id": 1, "merged": False, "alerts": 0}
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield ServerClient(BASE, "secret-key-123", http=http)


def _parts(req: httpx.Request) -> dict[str, tuple[str, bytes]]:
    """name -> (content type, body) of a multipart request."""
    ctype = req.headers["content-type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=")[1].encode()
    out = {}
    for chunk in req.content.split(b"--" + boundary)[1:-1]:
        head, _, body = chunk.partition(b"\r\n\r\n")
        body = body.removesuffix(b"\r\n")
        headers = head.decode("latin-1")
        name = headers.split('name="')[1].split('"')[0]
        ct = ""
        for line in headers.splitlines():
            if line.lower().startswith("content-type:"):
                ct = line.split(":", 1)[1].strip()
        out[name] = (ct, body)
    return out


# --- config -----------------------------------------------------------------------------


@respx.mock
async def test_config_defaults_without_new_keys(client):
    respx.get(f"{BASE}/device/config").respond(
        200, json={"enabled_sounds": ["doorbell"], "heartbeat_interval_s": 60}
    )
    cfg = await client.config()
    assert cfg.guarding is False
    assert cfg.send_snapshots is True
    assert cfg == DeviceConfig(frozenset({"doorbell"}), 60)


@respx.mock
async def test_config_reads_guarding_and_send_snapshots(client):
    body = {
        "enabled_sounds": ["fall"],
        "heartbeat_interval_s": 30,
        "guarding": True,
        "send_snapshots": False,
    }
    respx.get(f"{BASE}/device/config").respond(200, json=body)
    respx.post(f"{BASE}/device/heartbeat").respond(200, json=body)
    for cfg in (await client.config(), await client.heartbeat()):
        assert cfg.guarding is True
        assert cfg.send_snapshots is False


@respx.mock
async def test_config_non_bool_values_use_defaults(client):
    respx.get(f"{BASE}/device/config").respond(
        200,
        json={
            "enabled_sounds": [],
            "heartbeat_interval_s": 60,
            "guarding": "yes",
            "send_snapshots": None,
        },
    )
    cfg = await client.config()
    assert (cfg.guarding, cfg.send_snapshots) == (False, True)


# --- client -----------------------------------------------------------------------------


@respx.mock
async def test_send_event_without_snapshot_is_plain_json(client):
    route = respx.post(f"{BASE}/device/events").respond(202, json=OK)
    await client.send_event("fall", 0.9, NOW)
    req = route.calls[0].request
    assert req.headers["content-type"] == "application/json"
    assert (
        req.content
        == json.dumps(
            {"type": "fall", "confidence": 0.9, "occurred_at": NOW.isoformat()},
            separators=(",", ":"),
        ).encode()
    )
    assert req.headers["X-Device-Key"] == "secret-key-123"


@respx.mock
async def test_send_event_with_snapshot_is_multipart(client):
    route = respx.post(f"{BASE}/device/events").respond(202, json=OK)
    res = await client.send_event("fall", 0.9, NOW, snapshot=JPEG)
    assert res.event_id == 1
    req = route.calls[0].request
    assert req.headers["X-Device-Key"] == "secret-key-123"
    assert req.headers["content-length"]
    parts = _parts(req)
    assert set(parts) == {"event", "snapshot"}
    ctype, body = parts["event"]
    assert ctype == "application/json"
    assert json.loads(body) == {
        "type": "fall",
        "confidence": 0.9,
        "occurred_at": NOW.isoformat(),
    }
    assert parts["snapshot"] == ("image/jpeg", JPEG)


# --- queue ------------------------------------------------------------------------------


class FakeClient:
    """send_event outcomes are scripted; records (sound, snapshot) per call."""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, bytes | None]] = []

    async def send_event(self, sound, confidence, occurred_at, snapshot=None):
        self.calls.append((sound, snapshot))
        out = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(out, Exception):
            raise out
        return out


def _q() -> EventQueue:
    return EventQueue(clock=lambda: 0.0)


async def test_drain_passes_snapshot_through():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    q.put("doorbell", 0.9, NOW)
    c = FakeClient()
    assert await q.drain(c, NOW) == 2
    assert c.calls == [("fall", JPEG), ("doorbell", None)]


async def test_drain_without_snapshot_does_not_pass_the_keyword():
    """Duck-typed clients that predate snapshots keep working."""

    class Old:
        calls = 0

        async def send_event(self, sound, confidence, occurred_at):
            self.calls += 1

    q = _q()
    q.put("fall", 0.9, NOW)
    c = Old()
    assert await q.drain(c, NOW) == 1 and c.calls == 1


async def test_at_most_five_queued_images_oldest_lose_theirs_first():
    q = _q()
    for i in range(7):
        q.put(f"e{i}", 0.9, NOW, bytes([i]) * 4)
    assert len(q) == 7  # events are all kept
    c = FakeClient()
    await q.drain(c, NOW)
    assert [s for _, s in c.calls] == [None, None] + [bytes([i]) * 4 for i in range(2, 7)]


async def test_image_dropped_after_three_failed_sends_event_delivered():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([ServerUnavailable("x"), ServerUnavailable("z"), ServerUnavailable("y")])
    for exc in (ServerUnavailable, ServerUnavailable, ServerUnavailable):
        with pytest.raises(exc):
            await q.drain(c, NOW)
    assert len(q) == 1
    assert await q.drain(c, NOW) == 1
    assert [s for _, s in c.calls] == [JPEG, JPEG, JPEG, None]


async def test_rate_limited_does_not_count_against_the_image():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([RateLimited(1)] * 4 + [None])
    for _ in range(4):
        with pytest.raises(RateLimited):
            await q.drain(c, NOW)
    assert await q.drain(c, NOW) == 1
    assert [s for _, s in c.calls] == [JPEG] * 5


async def test_image_kept_after_two_failures():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([ServerUnavailable("x"), ServerUnavailable("y")])
    for _ in range(2):
        with pytest.raises(ServerUnavailable):
            await q.drain(c, NOW)
    assert await q.drain(c, NOW) == 1
    assert [s for _, s in c.calls] == [JPEG, JPEG, JPEG]


async def test_image_dropped_when_event_older_than_60s():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient()
    assert await q.drain(c, NOW + timedelta(seconds=61)) == 1
    assert c.calls == [("fall", None)]


async def test_image_kept_at_60s():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient()
    await q.drain(c, NOW + timedelta(seconds=60))
    assert c.calls == [("fall", JPEG)]


@pytest.mark.parametrize("status", [400, 411, 413, 415, 422])
async def test_4xx_on_image_retries_same_event_once_without_image(status):
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([Rejected("too big", status), None])
    assert await q.drain(c, NOW) == 1
    assert c.calls == [("fall", JPEG), ("fall", None)]
    assert len(q) == 0


async def test_4xx_without_image_is_not_retried():
    q = _q()
    q.put("fall", 0.9, NOW)
    c = FakeClient([Rejected("bad", 422)])
    assert await q.drain(c, NOW) == 0
    assert c.calls == [("fall", None)]


async def test_4xx_on_both_attempts_drops_event_once():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([Rejected("a", 413), Rejected("b", 422)])
    assert await q.drain(c, NOW) == 0
    assert len(c.calls) == 2 and len(q) == 0


async def test_retry_without_image_failing_transiently_keeps_event_without_image():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([Rejected("big", 413), ServerUnavailable("x"), None])
    with pytest.raises(ServerUnavailable):
        await q.drain(c, NOW)
    assert len(q) == 1
    assert await q.drain(c, NOW) == 1
    # the server refused the image version once; it is never sent with the image again
    assert [s for _, s in c.calls] == [JPEG, None, None]


async def test_5xx_and_429_keep_image_and_event():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([RateLimited(2), None])
    with pytest.raises(RateLimited):
        await q.drain(c, NOW)
    assert len(q) == 1
    assert await q.drain(c, NOW) == 1
    assert [s for _, s in c.calls] == [JPEG, JPEG]


async def test_snapshot_bytes_never_logged(caplog):
    caplog.set_level("DEBUG")
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([Rejected("too big", 413), None])
    await q.drain(c, NOW)
    assert "fake-jpeg" not in caplog.text and repr(JPEG) not in caplog.text


@pytest.mark.parametrize("status", [403, 404, 409])
async def test_other_4xx_is_not_retried_without_image(status):
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([Rejected("nope", status)])
    assert await q.drain(c, NOW) == 0
    assert c.calls == [("fall", JPEG)] and len(q) == 0


async def test_first_refusal_status_is_logged_without_bytes(caplog):
    caplog.set_level("DEBUG")
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    await q.drain(FakeClient([Rejected("big", 413), None]), NOW)
    assert "HTTP 413" in caplog.text and "fake-jpeg" not in caplog.text


async def test_401_with_image_raises_revoked_and_keeps_item():
    q = _q()
    q.put("fall", 0.9, NOW, JPEG)
    c = FakeClient([DeviceRevoked()])
    with pytest.raises(DeviceRevoked):
        await q.drain(c, NOW)
    assert len(q) == 1 and c.calls == [("fall", JPEG)]


def test_reprs_never_contain_snapshot_bytes(caplog):
    caplog.set_level("DEBUG")
    secret = b"SECRETJPEG"
    q = _q()
    q.put("fall", 0.9, NOW, secret)
    item = q._items[0]
    import logging

    logging.getLogger("t").warning("%r %s", item, item)
    for text in (repr(item), str(item), repr(q._items[0]), repr(q._items), caplog.text):
        assert "SECRETJPEG" not in text and "SECRET" not in text and repr(secret) not in text


@respx.mock
async def test_multipart_parts_have_exact_content_types(client):
    route = respx.post(f"{BASE}/device/events").respond(202, json=OK)
    await client.send_event("fall", 0.9, NOW, snapshot=JPEG)
    parts = _parts(route.calls[0].request)
    assert {k: v[0] for k, v in parts.items()} == {
        "event": "application/json",
        "snapshot": "image/jpeg",
    }


async def test_put_clearing_head_snapshot_during_send_is_harmless():
    q = _q()
    q.put("head", 0.9, NOW, JPEG)

    class Client(FakeClient):
        async def send_event(self, sound, confidence, occurred_at, snapshot=None):
            if sound == "head" and snapshot is not None:
                for i in range(5):  # pushes the head's image out of the 5-image cap
                    q.put(f"n{i}", 0.9, NOW, bytes([i + 1]) * 3)
            return await super().send_event(sound, confidence, occurred_at, snapshot)

    c = Client([Rejected("big", 413), None])
    assert await q.drain(c, NOW) == 6
    assert c.calls[0] == ("head", JPEG) and c.calls[1] == ("head", None)
    assert len(q) == 0
