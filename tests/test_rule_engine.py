import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.alerts.models import Alert
from app.auth.models import Member
from app.devices.models import Device
from app.events.models import Event
from app.rules.engine import process_event
from app.rules.models import SoundSetting
from tests.test_events import _body, _device


async def _setup(signed_up, session, devices=1):
    owner, _ = await signed_up()
    ids = []
    for i in range(devices):
        d = Device(
            household_id=owner.household_id, name=f"d{i}", kind="pi", key_hash=f"hash{i}x{owner.id}"
        )
        session.add(d)
        await session.flush()
        ids.append(d.id)
    return owner, ids


async def _fire(session, redis, owner, device_id, sound, now):
    now_dt = datetime.now(UTC)
    ev = Event(
        household_id=owner.household_id,
        device_id=device_id,
        sound_key=sound,
        confidence=0.9,
        occurred_at=now_dt,
        received_at=now_dt,
    )
    session.add(ev)
    await session.flush()
    return ev, await process_event(session, redis, ev, now)


async def _set(session, member, sound, enabled=True, repeat=1, window=60, cooldown=120):
    session.add(
        SoundSetting(
            household_id=member.household_id,
            member_id=member.id,
            sound_key=sound,
            enabled=enabled,
            repeat_count=repeat,
            window_s=window,
            cooldown_s=cooldown,
        )
    )
    await session.flush()


async def test_single_scream_alerts_owner_with_default_settings(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    ev, out = await _fire(session, redis, owner, dev, "scream", 1000.0)
    assert out.merged is False and len(out.alert_ids) == 1
    alert = await session.get(Alert, out.alert_ids[0])
    assert alert.member_id == owner.id and alert.household_id == owner.household_id
    assert alert.event_id == ev.id and alert.status == "pending" and alert.sound_key == "scream"


async def test_disabled_sound_creates_no_alert(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    _, out = await _fire(session, redis, owner, dev, "doorbell", 1000.0)  # default off
    assert out.alert_ids == [] and out.merged is False
    await _set(session, owner, "scream", enabled=False)
    _, out = await _fire(session, redis, owner, dev, "scream", 1001.0)
    assert out.alert_ids == []


async def test_dog_barking_needs_5_within_60s(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    await _set(session, owner, "dog_barking", repeat=5)  # off by default; default repeat is 5
    for t in (0, 10, 20, 30):
        _, out = await _fire(session, redis, owner, dev, "dog_barking", 1000.0 + t)
        assert out.alert_ids == []
    _, out = await _fire(session, redis, owner, dev, "dog_barking", 1050.0)
    assert len(out.alert_ids) == 1


async def test_dog_barking_spread_out_does_not_alert(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    await _set(session, owner, "dog_barking", repeat=5)  # off by default; default repeat is 5
    for t in (0, 60, 120, 180, 240):
        _, out = await _fire(session, redis, owner, dev, "dog_barking", 1000.0 + t)
        assert out.alert_ids == []


async def test_cooldown_suppresses_then_expires(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    await _set(session, owner, "scream", cooldown=1)
    _, out = await _fire(session, redis, owner, dev, "scream", 1000.0)
    assert len(out.alert_ids) == 1
    await asyncio.sleep(0.2)
    _, out = await _fire(session, redis, owner, dev, "scream", 1030.0)
    assert out.alert_ids == []  # still cooling down
    await asyncio.sleep(1.0)
    _, out = await _fire(session, redis, owner, dev, "scream", 1060.0)
    assert len(out.alert_ids) == 1


async def test_same_scream_on_two_devices_merges(signed_up, session, redis):
    owner, (a, b) = await _setup(signed_up, session, devices=2)
    e1, o1 = await _fire(session, redis, owner, a, "scream", 1000.0)
    e2, o2 = await _fire(session, redis, owner, b, "scream", 1001.0)
    assert o1.merged is False and len(o1.alert_ids) == 1
    assert o2.merged is True and o2.alert_ids == [] and e2.merged is True
    assert len((await session.scalars(select(Alert))).all()) == 1


async def test_same_device_repeats_are_not_merged(signed_up, session, redis):
    owner, (a,) = await _setup(signed_up, session)
    await _set(session, owner, "scream", repeat=2)
    _, o1 = await _fire(session, redis, owner, a, "scream", 1000.0)
    _, o2 = await _fire(session, redis, owner, a, "scream", 1001.0)
    assert o1.merged is False and o2.merged is False
    assert o1.alert_ids == [] and len(o2.alert_ids) == 1  # second counted toward repeat


async def test_each_member_gets_own_alert_and_rules(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    other = Member(
        household_id=owner.household_id,
        email="m2@example.com",
        password_hash="x",
        role="member",
        email_confirmed=True,
    )
    session.add(other)
    await session.flush()
    await _set(session, other, "scream", repeat=3)
    _, out = await _fire(session, redis, owner, dev, "scream", 1000.0)
    alerts = [await session.get(Alert, i) for i in out.alert_ids]
    assert [a.member_id for a in alerts] == [owner.id]


async def test_event_route_enqueues_alerts_after_commit(
    client, signed_up, session, jobs, monkeypatch
):
    owner, h = await signed_up()
    _, dk = await _device(client, h)
    order: list[str] = []
    real_commit, real_enqueue = session.commit, jobs.enqueue_alert

    async def commit():
        order.append("commit")
        await real_commit()

    async def enqueue(alert_id):
        order.append(f"enqueue:{alert_id}")
        await real_enqueue(alert_id)

    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(jobs, "enqueue_alert", enqueue)
    r = await client.post("/device/events", json=_body("scream"), headers=dk)
    assert r.status_code == 202 and r.json()["alerts"] == 1
    alerts = (await session.scalars(select(Alert))).all()
    assert len(alerts) == 1 and jobs.enqueued == [alerts[0].id]
    assert alerts[0].member_id == owner.id
    assert order == ["commit", f"enqueue:{alerts[0].id}"]  # commit strictly before enqueue


async def test_zero_cooldown_still_alerts(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    await _set(session, owner, "scream", cooldown=0)
    _, out = await _fire(session, redis, owner, dev, "scream", 1000.0)
    assert len(out.alert_ids) == 1


async def test_dog_barking_catalog_defaults_need_5_barks(signed_up, session, redis):
    from app.catalog import get_sound

    snd = get_sound("dog_barking")
    assert snd.default_repeat == 5
    owner, (dev,) = await _setup(signed_up, session)
    await _set(
        session,
        owner,
        "dog_barking",
        repeat=snd.default_repeat,
        window=snd.default_window_s,
        cooldown=snd.default_cooldown_s,
    )
    for t in range(4):
        _, out = await _fire(session, redis, owner, dev, "dog_barking", 1000.0 + t)
        assert out.alert_ids == []
    _, out = await _fire(session, redis, owner, dev, "dog_barking", 1004.0)
    assert len(out.alert_ids) == 1


async def test_same_device_refreshes_merge_window(signed_up, session, redis):
    owner, (a, b) = await _setup(signed_up, session, devices=2)
    await _set(session, owner, "scream", repeat=3)
    _, o1 = await _fire(session, redis, owner, a, "scream", 1000.0)
    _, o2 = await _fire(session, redis, owner, a, "scream", 1001.0)
    assert o1.merged is False and o2.merged is False
    e3, o3 = await _fire(session, redis, owner, b, "scream", 1002.0)
    assert o3.merged is True and e3.merged is True and o3.alert_ids == []
    assert await redis.get(f"merge:{owner.household_id}:scream") == str(a)
