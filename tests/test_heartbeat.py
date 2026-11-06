from datetime import UTC, datetime

import pytest
from sqlalchemy import update

import app.devices.router as devices_router
from app.devices.models import Device
from tests.test_devices import _pair

DEFAULT_ON = ["glass_breaking", "scream", "smoke_alarm"]
DEFAULT_THRESHOLDS = {"glass_breaking": 0.30, "scream": 0.30, "smoke_alarm": 0.30}


pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")


async def _device(client, headers, **kw):
    paired = await _pair(client, headers, **kw)
    return paired["device_id"], {"X-Device-Key": paired["device_key"]}


async def test_heartbeat_marks_online_and_returns_union_config(client, signed_up, session):
    _, h = await signed_up()
    device_id, dk = await _device(client, h)
    before = datetime.now(UTC)
    r = await client.post("/device/heartbeat", headers=dk)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "enabled_sounds": DEFAULT_ON,
        "heartbeat_interval_s": 60,
        "thresholds": DEFAULT_THRESHOLDS,
    }
    device = await session.get(Device, device_id)
    await session.refresh(device)
    assert device.status == "online" and device.last_seen_at >= before
    assert (await client.post("/device/heartbeat")).status_code == 401


async def test_offline_device_publishes_device_online(client, signed_up, session, monkeypatch):
    _, h = await signed_up()
    device_id, dk = await _device(client, h, name="Hall")
    published = []

    async def fake_publish(redis, household_id, msg):
        published.append((household_id, msg))

    monkeypatch.setattr(devices_router, "publish_to_household", fake_publish)
    await client.post("/device/heartbeat", headers=dk)
    assert published == []  # pending -> online does not publish
    await session.execute(update(Device).values(status="offline"))
    await client.post("/device/heartbeat", headers=dk)
    assert len(published) == 1
    assert published[0][1] == {"kind": "device_online", "device_id": device_id, "name": "Hall"}
    await client.post("/device/heartbeat", headers=dk)
    assert len(published) == 1  # online -> online does not publish


async def test_config_reflects_setting_change_on_next_heartbeat(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    await client.post("/device/heartbeat", headers=dk)
    body = {"enabled": True, "repeat_count": 5, "window_s": 60, "cooldown_s": 120}
    assert (
        await client.put("/me/sound-settings/dog_barking", json=body, headers=h)
    ).status_code == 200
    r = await client.post("/device/heartbeat", headers=dk)
    assert r.json()["enabled_sounds"] == sorted([*DEFAULT_ON, "dog_barking"])


async def test_get_config_does_not_touch_last_seen_or_limit(client, signed_up, session):
    _, h = await signed_up()
    device_id, dk = await _device(client, h)
    for _ in range(6):
        r = await client.get("/device/config", headers=dk)
        assert r.status_code == 200
        assert r.json() == {
            "enabled_sounds": DEFAULT_ON,
            "heartbeat_interval_s": 60,
            "thresholds": DEFAULT_THRESHOLDS,
        }
    device = await session.get(Device, device_id)
    await session.refresh(device)
    assert device.last_seen_at is None and device.status == "pending"
    assert (await client.get("/device/config")).status_code == 401


async def test_heartbeat_rate_limit_5th_in_minute_429(client, signed_up):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    _, dk2 = await _device(client, h, name="Other")
    for _ in range(4):
        assert (await client.post("/device/heartbeat", headers=dk)).status_code == 200
    assert (await client.post("/device/heartbeat", headers=dk)).status_code == 429
    # limit is per device
    assert (await client.post("/device/heartbeat", headers=dk2)).status_code == 200


async def test_heartbeat_uses_one_redis_command(client, signed_up, redis):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    assert (await client.post("/device/heartbeat", headers=dk)).status_code == 200  # now online

    executed: list[tuple[str, ...]] = []
    orig_execute_command = redis.execute_command
    orig_pipeline = redis.pipeline

    async def spy_execute_command(*args, **kwargs):
        executed.append(("direct", str(args[0]).upper()))
        return await orig_execute_command(*args, **kwargs)

    def spy_pipeline(*args, **kwargs):
        pipe = orig_pipeline(*args, **kwargs)
        orig_exec = pipe.execute

        async def spy_exec(*a, **k):
            executed.extend(("pipeline", str(c[0][0]).upper()) for c in pipe.command_stack)
            return await orig_exec(*a, **k)

        pipe.execute = spy_exec
        return pipe

    redis.execute_command = spy_execute_command
    redis.pipeline = spy_pipeline
    try:
        r = await client.post("/device/heartbeat", headers=dk)
    finally:
        redis.execute_command = orig_execute_command
        redis.pipeline = orig_pipeline
    assert r.status_code == 200
    # redis-py sends INCR as INCRBY name 1.
    assert executed == [("pipeline", "INCRBY"), ("pipeline", "EXPIRE")]
