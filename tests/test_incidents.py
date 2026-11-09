import json
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import app.events.router as events_router
import app.snapshots.store as store
from app.alerts.models import Alert
from app.events.models import Event
from app.households.models import Household
from app.rules.incidents import check_incidents
from app.snapshots.models import Snapshot
from tests.doubles import make_jpeg

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")

JPEG = make_jpeg()
T0 = 1_000_000.0


async def _room(client, h, name):
    r = await client.post("/rooms", json={"name": name, "kind": "common"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _device(client, h, name="Mic", *, room_id=None, camera=False):
    body = {"name": name, "kind": "camera" if camera else "laptop"}
    if camera:
        body |= {"capabilities": "video", "consent_confirmed": True}
    if room_id is not None:
        body["room_id"] = room_id
    r = await client.post("/devices/pairing-codes", json=body, headers=h)
    assert r.status_code == 201, r.text
    r = await client.post("/device/pair", json={"code": r.json()["code"]})
    assert r.status_code == 201, r.text
    return r.json()["device_id"], {"X-Device-Key": r.json()["device_key"]}


async def _event(session, household_id, device_id, key, conf=0.9, merged=False):
    now = datetime.now(UTC)
    e = Event(
        household_id=household_id,
        device_id=device_id,
        sound_key=key,
        confidence=conf,
        occurred_at=now,
        received_at=now,
        merged=merged,
    )
    session.add(e)
    await session.flush()
    return e


async def _feed(session, redis, hid, dev, key, at, conf=0.9, **kw):
    e = await _event(session, hid, dev, key, conf, **kw)
    ids = await check_incidents(session, redis, e, T0 + at)
    return e, ids


async def _incident_events(session, key=None):
    q = select(Event).where(Event.sound_key.like("incident_%"))
    if key:
        q = select(Event).where(Event.sound_key == key)
    return (await session.scalars(q)).all()


async def test_glass_then_person_away_raises_break_in_with_camera_snapshot(
    client, session, redis, signed_up
):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    mic, _ = await _device(client, h, "Mic", room_id=room)
    cam, _ = await _device(client, h, "Cam", room_id=room, camera=True)
    hid = member.household_id
    await _feed(session, redis, hid, mic, "glass_breaking", 0, conf=0.8)
    away = await _event(session, hid, cam, "person_away", 0.95)
    snap = await store.save(session, hid, away.id, JPEG)
    assert snap is not None
    ids = await check_incidents(session, redis, away, T0 + 5)
    assert len(ids) == 1
    (inc,) = await _incident_events(session, "incident_break_in")
    assert inc.device_id == cam and inc.confidence == 0.95 and inc.merged is False
    alert = await session.get(Alert, ids[0])
    assert alert.event_id == inc.id and alert.sound_key == "incident_break_in"
    # a new row with its own copy of the file, same picture
    copy = await session.scalar(select(Snapshot).where(Snapshot.event_id == inc.id))
    assert copy is not None and copy.id != snap.id and copy.path != snap.path
    assert store.load(copy) == JPEG
    r = await client.get(f"/snapshots/{inc.id}", headers=h)
    assert r.status_code == 200 and r.content == JPEG
    # the original is untouched
    r = await client.get(f"/snapshots/{away.id}", headers=h)
    assert r.status_code == 200


async def test_incident_alert_carries_camera_room(client, session, redis, signed_up):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    mic, _ = await _device(client, h, "Mic", room_id=room)
    cam, _ = await _device(client, h, "Cam", room_id=room, camera=True)
    await _feed(session, redis, member.household_id, mic, "glass_breaking", 0)
    _, ids = await _feed(session, redis, member.household_id, cam, "person_away", 3)
    alert = await session.get(Alert, ids[0])
    # the alert's event is the incident event on the camera: its device (and so its room) is
    # what delivery and GET /alerts resolve
    rows = (await client.get("/alerts", headers=h)).json()
    assert [r["id"] for r in rows] == [alert.id]
    assert rows[0]["device_name"] == "Cam" and rows[0]["room_name"] == "Hall"
    events = {e["sound_key"]: e for e in (await client.get("/events", headers=h)).json()}
    assert events["incident_break_in"]["room_name"] == "Hall"


async def test_reversed_order_also_works(client, session, redis, signed_up):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    _, ids = await _feed(session, redis, member.household_id, dev, "person_away", 0)
    assert ids == []
    _, ids = await _feed(session, redis, member.household_id, dev, "glass_breaking", 14)
    assert len(ids) == 1
    assert len(await _incident_events(session, "incident_break_in")) == 1


async def test_too_far_apart_no_incident(client, session, redis, signed_up):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    await _feed(session, redis, member.household_id, dev, "glass_breaking", 0)
    _, ids = await _feed(session, redis, member.household_id, dev, "person_away", 16)
    assert ids == [] and await _incident_events(session) == []


async def test_different_rooms_no_incident(client, session, redis, signed_up):
    member, h = await signed_up()
    a, b = await _room(client, h, "A"), await _room(client, h, "B")
    da, _ = await _device(client, h, "Da", room_id=a)
    db, _ = await _device(client, h, "Db", room_id=b)
    await _feed(session, redis, member.household_id, da, "glass_breaking", 0)
    _, ids = await _feed(session, redis, member.household_id, db, "person_away", 2)
    assert ids == [] and await _incident_events(session) == []


async def test_devices_without_room_never_participate(client, session, redis, signed_up):
    member, h = await signed_up()
    d1, _ = await _device(client, h, "D1")
    d2, _ = await _device(client, h, "D2")
    await _feed(session, redis, member.household_id, d1, "glass_breaking", 0)
    _, ids = await _feed(session, redis, member.household_id, d2, "person_away", 1)
    assert ids == [] and await _incident_events(session) == []


async def test_fall_plus_scream_or_thud_is_fall_confirmed(client, session, redis, signed_up):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    hid = member.household_id
    await _feed(session, redis, hid, dev, "fall", 0)
    _, ids = await _feed(session, redis, hid, dev, "scream", 9)
    assert len(ids) == 1
    assert len(await _incident_events(session, "incident_fall_confirmed")) == 1
    # thud too, in another room
    room2 = await _room(client, h, "Den")
    dev2, _ = await _device(client, h, "D2", room_id=room2)
    await _feed(session, redis, hid, dev2, "thud_fall", 100)
    await _feed(session, redis, hid, dev2, "fall", 105)
    assert len(await _incident_events(session, "incident_fall_confirmed")) == 2


async def test_fall_and_scream_11s_apart_no_incident(client, session, redis, signed_up):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    await _feed(session, redis, member.household_id, dev, "fall", 0)
    _, ids = await _feed(session, redis, member.household_id, dev, "scream", 11)
    assert ids == []


async def test_repeat_within_60s_fires_once(client, session, redis, signed_up):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    hid = member.household_id
    await _feed(session, redis, hid, dev, "glass_breaking", 0)
    _, first = await _feed(session, redis, hid, dev, "person_away", 1)
    await _feed(session, redis, hid, dev, "glass_breaking", 20)
    _, again = await _feed(session, redis, hid, dev, "person_away", 21)
    assert len(first) == 1 and again == []
    assert len(await _incident_events(session, "incident_break_in")) == 1


async def test_member_who_disabled_incident_key_not_alerted(client, session, redis, signed_up):
    member, h = await signed_up()
    body = {"enabled": False, "repeat_count": 1, "window_s": 60, "cooldown_s": 120}
    r = await client.put("/me/sound-settings/incident_break_in", json=body, headers=h)
    assert r.status_code == 200, r.text
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    await _feed(session, redis, member.household_id, dev, "glass_breaking", 0)
    _, ids = await _feed(session, redis, member.household_id, dev, "person_away", 1)
    assert ids == []


async def test_merged_ingredient_still_counts(client, session, redis, signed_up):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    await _feed(session, redis, member.household_id, dev, "glass_breaking", 0, merged=True)
    _, ids = await _feed(session, redis, member.household_id, dev, "person_away", 1)
    assert len(ids) == 1


async def test_break_in_wins_over_property_damage(client, session, redis, signed_up):
    member, h = await signed_up()
    await client.patch("/households/me", json={"away": True}, headers=h)
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    await _feed(session, redis, member.household_id, dev, "glass_breaking", 0)
    _, ids = await _feed(session, redis, member.household_id, dev, "person_away", 2)
    assert len(ids) == 1
    kinds = {e.sound_key for e in await _incident_events(session)}
    assert kinds == {"incident_break_in"}


async def test_property_damage_only_while_guarding(client, session, redis, signed_up):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    dev, _ = await _device(client, h, room_id=room)
    hid = member.household_id
    await _feed(session, redis, hid, dev, "explosion", 0)
    _, ids = await _feed(session, redis, hid, dev, "person_away", 1)
    assert ids == []  # not Away, no night window
    household = await session.get(Household, hid)
    household.away = True
    await session.flush()
    await _feed(session, redis, hid, dev, "door_slam", 100)
    _, ids = await _feed(session, redis, hid, dev, "person_away", 101)
    assert len(ids) == 1
    assert len(await _incident_events(session, "incident_property_damage")) == 1


async def test_http_ingredients_alert_immediately_and_incident_follows(
    client, session, redis, signed_up, jobs
):
    member, h = await signed_up()
    assert (await client.patch("/households/me", json={"away": True}, headers=h)).status_code == 200
    room = await _room(client, h, "Hall")
    _, mic = await _device(client, h, "Mic", room_id=room)
    _, cam = await _device(client, h, "Cam", room_id=room, camera=True)

    def ev(t):
        return {"type": t, "confidence": 0.95, "occurred_at": datetime.now(UTC).isoformat()}

    r1 = await client.post("/device/events", json=ev("glass_breaking"), headers=mic)
    assert r1.status_code == 202 and r1.json()["alerts"] == 1
    files = {
        "event": (None, json.dumps(ev("person_away"))),
        "snapshot": ("s.jpg", JPEG, "image/jpeg"),
    }
    r2 = await client.post("/device/events", files=files, headers=cam)
    assert r2.status_code == 202
    assert r2.json()["alerts"] == 1  # the ingredient's own alert; the incident is separate
    (inc,) = await _incident_events(session, "incident_break_in")
    alerts = (await session.scalars(select(Alert).where(Alert.event_id == inc.id))).all()
    assert len(alerts) == 1
    assert alerts[0].id in jobs.enqueued  # follow-up alert enqueued after its own commit
    assert (await client.get(f"/snapshots/{inc.id}", headers=h)).content == JPEG


async def test_incident_failure_never_breaks_the_ingredient_response(
    client, session, redis, signed_up, monkeypatch, caplog
):
    member, h = await signed_up()
    room = await _room(client, h, "Hall")
    _, mic = await _device(client, h, room_id=room)

    async def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(events_router, "check_incidents", boom)
    body = {
        "type": "glass_breaking",
        "confidence": 0.95,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    r = await client.post("/device/events", json=body, headers=mic)
    assert r.status_code == 202 and r.json()["alerts"] == 1
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text
    assert time.time() > 0


async def test_incident_failure_after_sql_keeps_ingredient_body_and_keys(
    client, session, redis, signed_up, monkeypatch
):
    """A real SQL call, then a failure: the rollback expires the ingredient event, but the
    response (built before) is intact, no incident row is left, and the NX key is undone."""
    import app.rules.incidents as incidents

    member, h = await signed_up()
    hid = member.household_id  # read now: the rollback under test expires `member`
    assert (await client.patch("/households/me", json={"away": True}, headers=h)).status_code == 200
    room = await _room(client, h, "Hall")
    dev, mic = await _device(client, h, room_id=room)
    real = incidents.process_event

    async def boom(sess, *a, **k):
        await sess.get(Household, hid, populate_existing=True)
        raise RuntimeError("late failure")

    def ev(t):
        return {"type": t, "confidence": 0.95, "occurred_at": datetime.now(UTC).isoformat()}

    monkeypatch.setattr(incidents, "process_event", boom)
    r1 = await client.post("/device/events", json=ev("glass_breaking"), headers=mic)
    r2 = await client.post("/device/events", json=ev("person_away"), headers=mic)
    assert r2.status_code == 202, r2.text
    body = r2.json()
    assert body["event_id"] > r1.json()["event_id"] and body["merged"] is False
    assert body["alerts"] == 1
    assert await _incident_events(session) == []
    assert await redis.get(f"incfired:{hid}:{room}:incident_break_in") is None
    # the key was undone, so the next completing event can still raise it
    monkeypatch.setattr(incidents, "process_event", real)
    r3 = await client.post("/device/events", json=ev("glass_breaking"), headers=mic)
    assert r3.status_code == 202
    assert len(await _incident_events(session, "incident_break_in")) == 1


async def test_commit_failure_after_incident_keeps_ingredient_body(
    client, session, redis, signed_up, monkeypatch
):
    member, h = await signed_up()
    hid = member.household_id
    assert (await client.patch("/households/me", json={"away": True}, headers=h)).status_code == 200
    room = await _room(client, h, "Hall")
    _, mic = await _device(client, h, room_id=room)

    def ev(t):
        return {"type": t, "confidence": 0.95, "occurred_at": datetime.now(UTC).isoformat()}

    await client.post("/device/events", json=ev("glass_breaking"), headers=mic)
    real_commit = session.commit
    calls = []

    async def commit():  # the 1st commit is the ingredient's; the 2nd is the incident's
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("commit failed")
        await real_commit()

    monkeypatch.setattr(session, "commit", commit)
    r = await client.post("/device/events", json=ev("person_away"), headers=mic)
    assert r.status_code == 202 and r.json()["alerts"] == 1 and "event_id" in r.json()
    assert len(calls) == 2
    assert await redis.get(f"incfired:{hid}:{room}:incident_break_in") is None


async def test_incidents_in_two_rooms_within_5s_both_alert(client, session, redis, signed_up):
    member, h = await signed_up()
    body = {"enabled": True, "repeat_count": 1, "window_s": 60, "cooldown_s": 0}
    r = await client.put("/me/sound-settings/incident_break_in", json=body, headers=h)
    assert r.status_code == 200, r.text
    hid = member.household_id
    out = []
    for i, name in enumerate(("A", "B")):
        room = await _room(client, h, name)
        dev, _ = await _device(client, h, f"D{name}", room_id=room)
        await _feed(session, redis, hid, dev, "glass_breaking", i)
        _, ids = await _feed(session, redis, hid, dev, "person_away", i + 1)
        out += ids
        # per-member cooldown still applies by design (it lasts at least 1 s): clear it so
        # this test isolates the cross-device merge, which incidents bypass
        await redis.delete(f"cd:{hid}:{member.id}:incident_break_in")
    assert len(out) == 2
    assert not any(e.merged for e in await _incident_events(session, "incident_break_in"))
