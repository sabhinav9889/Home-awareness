import asyncio
import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import update

import app.devices.offline as offline
import app.main as app_main
from app.devices.models import Device
from app.devices.offline import check_offline_devices, offline_loop
from app.rules.models import AlertPreference

TOKEN = "123:SECRET-TOKEN"
TG_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SETTINGS = SimpleNamespace(telegram_bot_token=TOKEN, ntfy_base_url="https://ntfy.test")


_keys = itertools.count(1)


async def _device(session, member, *, name="Hall", status="online", age=200):
    n = next(_keys)
    d = Device(
        household_id=member.household_id, name=name, kind="pi", key_hash=f"off{n}",
        status=status, last_seen_at=None if status == "pending" else NOW - timedelta(seconds=age),
    )  # fmt: skip
    session.add(d)
    await session.flush()
    return d


async def _prefs(session, member, **kw):
    prefs = await session.get(AlertPreference, member.id)
    if prefs is None:
        prefs = AlertPreference(member_id=member.id, household_id=member.household_id)
        session.add(prefs)
    for k, v in kw.items():
        setattr(prefs, k, v)
    await session.flush()


async def test_device_marked_offline_after_180s_once(signed_up, session, redis):
    member, _ = await signed_up()
    stale = await _device(session, member, name="Stale", age=181)
    fresh = await _device(session, member, name="Fresh", age=179)
    assert await check_offline_devices(session, redis, NOW, settings=SETTINGS) == [stale.id]
    await session.refresh(stale)
    await session.refresh(fresh)
    assert (stale.status, fresh.status) == ("offline", "online")
    assert await check_offline_devices(session, redis, NOW, settings=SETTINGS) == []


async def test_pending_device_ignored(signed_up, session, redis):
    member, _ = await signed_up()
    d = await _device(session, member, status="pending")
    assert await check_offline_devices(session, redis, NOW, settings=SETTINGS) == []
    await session.refresh(d)
    assert d.status == "pending"


@respx.mock
async def test_offline_notifies_household_live_and_channels(
    signed_up, session, redis, monkeypatch, caplog
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="my-topic")
    other_member, _ = await signed_up(email="b@example.com", household_name="B")  # other household
    await _prefs(session, other_member, ntfy_topic="not-mine")
    device = await _device(session, member)
    live = []

    async def fake_publish(r, household_id, msg):
        live.append((household_id, msg))

    monkeypatch.setattr(offline, "publish_to_household", fake_publish)
    text = "📴 Sensor Hall is offline — alerts from it have stopped."
    tg = respx.post(TG_URL).mock(return_value=httpx.Response(500, text=f"boom {TOKEN}"))
    nt = respx.post("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    other = respx.post("https://ntfy.test/not-mine").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        ids = await check_offline_devices(session, redis, NOW, http=http, settings=SETTINGS)
    assert ids == [device.id]
    assert live == [
        (member.household_id, {"kind": "device_offline", "device_id": device.id, "name": "Hall"})
    ]
    assert tg.call_count == 1 and b"offline" in tg.calls[0].request.content
    assert text in tg.calls[0].request.content.decode()
    assert nt.call_count == 1 and nt.calls[0].request.content.decode() == text
    assert not other.called
    assert TOKEN not in caplog.text  # telegram failed (500) but was logged without the token


async def test_heartbeat_after_offline_marks_online_and_publishes_device_online(
    client, signed_up, session, redis, monkeypatch
):
    import app.devices.router as devices_router
    from tests.test_devices import _pair

    monkeypatch.setattr("app.ratelimit.time.time", lambda: 1_800_000_000.0)
    _, h = await signed_up()
    paired = await _pair(client, h, name="Hall")
    dk = {"X-Device-Key": paired["device_key"]}
    await client.post("/device/heartbeat", headers=dk)
    await session.execute(
        update(Device).values(last_seen_at=datetime.now(UTC) - timedelta(seconds=600))
    )
    assert await check_offline_devices(session, redis, datetime.now(UTC), settings=SETTINGS) == [
        paired["device_id"]
    ]
    published = []

    async def fake_publish(r, household_id, msg):
        published.append(msg)

    monkeypatch.setattr(devices_router, "publish_to_household", fake_publish)
    assert (await client.post("/device/heartbeat", headers=dk)).status_code == 200
    device = await session.get(Device, paired["device_id"])
    await session.refresh(device)
    assert device.status == "online"
    assert published == [
        {"kind": "device_online", "device_id": paired["device_id"], "name": "Hall"}
    ]


class _Ctx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


async def test_loop_survives_errors_and_cancels_cleanly(redis, monkeypatch):
    calls = []

    async def flaky(session, r, now, *, http=None, settings=None):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(offline, "check_offline_devices", flaky)
    task = asyncio.create_task(offline_loop(_Ctx, redis, interval_s=0.01))
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(calls) >= 3  # kept going after the first run raised
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(("runner", "starts"), [("inprocess", True), ("celery", False)])
async def test_lifespan_starts_loop_only_when_inprocess_and_opted_in(
    redis, monkeypatch, runner, starts
):
    started = []

    async def fake_loop(factory, r, **kw):
        started.append(True)
        await asyncio.sleep(3600)

    async def fake_redis():
        return redis

    monkeypatch.setattr(app_main, "offline_loop", fake_loop)
    monkeypatch.setattr(app_main, "get_redis", fake_redis)
    monkeypatch.setattr(app_main, "_sessionmaker", lambda: None)
    monkeypatch.setattr(app_main, "get_settings", lambda: SimpleNamespace(job_runner=runner))
    for opted_in, expect in ((True, starts), (False, False)):
        started.clear()
        app = app_main.create_app(run_background_jobs=opted_in)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.01)
            assert bool(started) is expect
