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
    await _device(session, other_member, name="Live", age=5)  # the API hears heartbeats
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
    assert nt.calls[0].request.headers["Priority"] == "high"
    assert nt.calls[0].request.headers["Tags"] == "warning"
    assert not other.called
    assert TOKEN not in caplog.text  # telegram failed (500) but was logged without the token


async def test_heartbeat_after_offline_marks_online_and_publishes_device_online(
    client, signed_up, session, redis, monkeypatch, frozen_ratelimit_clock
):
    import app.devices.router as devices_router
    from tests.test_devices import _pair

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

    async def flaky(session, r, now, *, process_started_at=None, http=None, settings=None):
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


@respx.mock
async def test_publish_failure_does_not_block_other_devices(signed_up, session, redis, monkeypatch):
    member, _ = await signed_up()
    await _prefs(session, member, ntfy_topic="my-topic")
    await _device(session, member, name="First")
    await _device(session, member, name="Second")
    other, _ = await signed_up(email="b@example.com", household_name="B")
    await _device(session, other, name="Live", age=5)  # the API hears heartbeats
    calls = []

    async def failing_publish(r, household_id, msg):
        calls.append(msg["name"])
        if len(calls) == 1:
            raise RuntimeError("redis exploded")

    monkeypatch.setattr(offline, "publish_to_household", failing_publish)
    nt = respx.post("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        ids = await check_offline_devices(session, redis, NOW, http=http, settings=SETTINGS)
    assert len(ids) == 2 and len(calls) == 2
    assert nt.call_count == 2  # channels still attempted for the device whose publish raised


async def test_no_marking_during_startup_grace(signed_up, session, redis):
    member, _ = await signed_up()
    stale = await _device(session, member, name="Stale", age=400)
    await _device(session, member, name="Live", age=10)  # the API is receiving heartbeats
    started = NOW - timedelta(seconds=179)
    got = await check_offline_devices(
        session, redis, NOW, process_started_at=started, settings=SETTINGS
    )
    assert got == []
    await session.refresh(stale)
    assert stale.status == "online"
    got = await check_offline_devices(
        session, redis, NOW, process_started_at=NOW - timedelta(seconds=180), settings=SETTINGS
    )
    assert got == [stale.id]


async def test_no_marking_when_no_device_anywhere_heartbeated_recently(signed_up, session, redis):
    # Every sensor (all households) silent for >180 s: the API itself wasn't receiving
    # heartbeats (downtime), so marking now would send false offline warnings.
    member, _ = await signed_up()
    other, _ = await signed_up(email="b@example.com", household_name="B")
    a = await _device(session, member, name="A", age=200)
    b = await _device(session, other, name="B", age=300)
    assert await check_offline_devices(session, redis, NOW, settings=SETTINGS) == []
    await session.refresh(a)
    assert a.status == "online"
    fresh = await _device(session, other, name="Fresh", age=-170)  # heard again at +170 s
    later = NOW + timedelta(seconds=180)  # after the post-silence quiet window (F2)
    got = await check_offline_devices(session, redis, later, settings=SETTINGS)
    assert sorted(got) == sorted([a.id, b.id]) and fresh.id not in got


async def test_long_fleet_silence_still_warns(signed_up, session, redis):
    # Decision (protects the person relying on alerts): a lone sensor, or a whole home losing
    # its internet, must still be reported. Once all sensors have been silent for more than
    # 10 minutes the guard stops suppressing (a recovered API would have heard them by then).
    member, _ = await signed_up()
    lone = await _device(session, member, name="Lone", age=601)
    assert await check_offline_devices(session, redis, NOW, settings=SETTINGS) == [lone.id]


async def test_loop_passes_its_start_time(redis, monkeypatch):
    seen = []

    async def spy(session, r, now, *, process_started_at=None, http=None, settings=None):
        seen.append((now, process_started_at))
        return []

    monkeypatch.setattr(offline, "check_offline_devices", spy)
    times = iter([NOW, NOW + timedelta(seconds=60)])
    task = asyncio.create_task(
        offline_loop(_Ctx, redis, interval_s=0.01, clock=lambda: next(times, NOW))
    )
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen[0] == (NOW + timedelta(seconds=60), NOW)


@respx.mock
async def test_back_online_message_on_external_channels(
    client, signed_up, session, redis, monkeypatch, frozen_ratelimit_clock, caplog
):
    from tests.test_devices import _pair

    member, h = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="my-topic")
    monkeypatch.setattr(offline, "get_settings", lambda: SETTINGS)
    paired = await _pair(client, h, name="Hall")
    dk = {"X-Device-Key": paired["device_key"]}
    tg = respx.post(TG_URL).mock(return_value=httpx.Response(500, text=f"boom {TOKEN}"))
    nt = respx.post("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    assert (await client.post("/device/heartbeat", headers=dk)).status_code == 200
    assert not tg.called and not nt.called  # pending -> online: nothing to recover from
    await session.execute(update(Device).values(status="offline"))
    assert (await client.post("/device/heartbeat", headers=dk)).status_code == 200  # tg failed
    text = "✅ Sensor Hall is back online."
    assert nt.call_count == 1 and nt.calls[0].request.content.decode() == text
    assert tg.call_count == 1 and text in tg.calls[0].request.content.decode()
    assert TOKEN not in caplog.text
    assert (await client.post("/device/heartbeat", headers=dk)).status_code == 200
    assert nt.call_count == 1  # online -> online: no repeat


async def test_celery_check_passes_worker_start_time(monkeypatch):
    # A whole-server restart restarts the worker too: its own start time gives the same
    # startup grace as the in-process loop, however long the server was down.
    import app.alerts.celery_app as celery_app

    seen = []

    async def spy(session, r, now, *, process_started_at=None, http=None, settings=None):
        seen.append(process_started_at)
        return []

    monkeypatch.setattr(offline, "check_offline_devices", spy)
    await celery_app._check_offline()
    assert seen == [celery_app.PROCESS_STARTED_AT]


async def _seen(session, device, at):
    await session.execute(update(Device).where(Device.id == device.id).values(last_seen_at=at))


async def test_full_window_to_heartbeat_after_fleet_silence(signed_up, session, redis):
    member, _ = await signed_up()
    dead = await _device(session, member, name="Dead", age=300)
    back = await _device(session, member, name="Back", age=300)
    assert await check_offline_devices(session, redis, NOW, settings=SETTINGS) == []  # silent
    await _seen(session, back, NOW + timedelta(seconds=30))  # the API hears a sensor again
    for t in (60, 120, 179):  # within 180 s of the silence: still nobody marked
        got = await check_offline_devices(
            session, redis, NOW + timedelta(seconds=t), settings=SETTINGS
        )
        assert got == [], t
    await _seen(session, back, NOW + timedelta(seconds=150))
    got = await check_offline_devices(
        session, redis, NOW + timedelta(seconds=180), settings=SETTINGS
    )
    assert got == [dead.id]  # a full window passed and it still hasn't heartbeated


async def test_failed_check_opens_a_quiet_window(signed_up, session, redis, monkeypatch):
    calls = []

    async def failing(session, r, now, *, process_started_at=None, http=None, settings=None):
        calls.append(now)
        raise RuntimeError("db down")

    monkeypatch.setattr(offline, "check_offline_devices", failing)
    task = asyncio.create_task(offline_loop(_Ctx, redis, interval_s=0.01, clock=lambda: NOW))
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()
    assert float(await redis.get(offline.QUIET_KEY)) == (NOW + timedelta(seconds=180)).timestamp()
    member, _ = await signed_up()
    stale = await _device(session, member, name="Stale", age=400)
    await _device(session, member, name="Live", age=-170)  # heard at +170 s
    later = NOW + timedelta(seconds=179)
    assert await check_offline_devices(session, redis, later, settings=SETTINGS) == []
    later = NOW + timedelta(seconds=180)
    assert await check_offline_devices(session, redis, later, settings=SETTINGS) == [stale.id]


async def test_celery_check_failure_opens_a_quiet_window(redis, monkeypatch):
    import app.alerts.celery_app as celery_app

    async def failing(session, r, now, *, process_started_at=None, http=None, settings=None):
        raise RuntimeError("db down")

    class _NoClose:  # the task closes its client; keep the shared fake usable
        def __getattr__(self, name):
            return getattr(redis, name)

        async def aclose(self):
            return None

    monkeypatch.setattr(offline, "check_offline_devices", failing)
    monkeypatch.setattr(celery_app.Redis, "from_url", lambda *a, **k: _NoClose())
    with pytest.raises(RuntimeError):
        await celery_app._check_offline()
    assert await redis.get(offline.QUIET_KEY) is not None


async def test_status_messages_skip_unconfirmed_members(signed_up, session):
    from app.auth.models import Member

    owner, _ = await signed_up()
    await _prefs(session, owner, ntfy_topic="owner-topic")
    pending = Member(
        household_id=owner.household_id, email="p@example.com", password_hash="x",
        role="member", email_confirmed=False,
    )  # fmt: skip
    session.add(pending)
    await session.flush()
    await _prefs(session, pending, ntfy_topic="pending-topic")
    got = await offline.household_recipients(session, owner.household_id, SETTINGS)
    assert [(r.member_id, r.address) for r in got] == [(owner.id, "owner-topic")]
