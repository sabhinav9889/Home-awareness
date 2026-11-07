"""Plan 5a final review fixes."""

import json
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import app.events.router as events_router
from app.alerts.models import Alert
from app.events.models import Event
from app.snapshots.models import Snapshot
from tests.doubles import make_jpeg
from tests.test_incidents import _device, _incident_events, _room
from tests.test_rooms import _device as _device_row
from tests.test_rooms import _pair_camera
from tests.test_rooms import _room as _make_room

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")
JPEG = make_jpeg()


def _ev(t, conf=0.95):
    return {"type": t, "confidence": conf, "occurred_at": datetime.now(UTC).isoformat()}


def _multipart(t):
    return {"event": (None, json.dumps(_ev(t))), "snapshot": ("s.jpg", JPEG, "image/jpeg")}


async def _away(client, h, on=True):
    r = await client.patch("/households/me", json={"away": on}, headers=h)
    assert r.status_code == 200, r.text


# 1
async def test_device_config_and_heartbeat_carry_send_snapshots(client, signed_up):
    _, h = await signed_up()
    dev = await _pair_camera(client, h)
    code = (
        await client.post(
            "/devices/pairing-codes",
            json={
                "name": "C2",
                "kind": "camera",
                "capabilities": "video",
                "consent_confirmed": True,
            },
            headers=h,
        )
    ).json()["code"]
    key = (await client.post("/device/pair", json={"code": code})).json()["device_key"]
    dk = {"X-Device-Key": key}
    assert (await client.get("/device/config", headers=dk)).json()["send_snapshots"] is True
    await client.patch(f"/devices/{dev}", json={"send_snapshots": False}, headers=h)
    devices = (await client.get("/devices", headers=h)).json()
    other = next(d["id"] for d in devices if d["id"] != dev)
    await client.patch(f"/devices/{other}", json={"send_snapshots": False}, headers=h)
    assert (await client.get("/device/config", headers=dk)).json()["send_snapshots"] is False
    hb = await client.post("/device/heartbeat", headers=dk)
    assert hb.json()["send_snapshots"] is False


# 2
async def test_assigning_camera_to_private_room_turns_snapshots_off(client, signed_up):
    _, h = await signed_up()
    bed = await _make_room(client, h, "Bed", "bedroom")
    hall = await _make_room(client, h, "Hall", "common")
    dev = await _pair_camera(client, h)
    r = await client.patch(f"/devices/{dev}", json={"room_id": hall["id"]}, headers=h)
    assert r.json()["send_snapshots"] is True
    r = await client.patch(f"/devices/{dev}", json={"room_id": bed["id"]}, headers=h)
    assert r.json()["send_snapshots"] is False
    r = await client.patch(f"/devices/{dev}", json={"send_snapshots": True}, headers=h)
    assert r.json()["send_snapshots"] is True  # the owner can turn it back on


async def test_retyping_room_to_private_turns_off_video_devices_only(client, signed_up):
    _, h = await signed_up()
    room = await _make_room(client, h, "Den", "common")
    cam = await _pair_camera(client, h, room_id=room["id"])
    code = (
        await client.post(
            "/devices/pairing-codes",
            json={"name": "Mic", "kind": "pi", "room_id": room["id"]},
            headers=h,
        )
    ).json()["code"]
    mic = (await client.post("/device/pair", json={"code": code})).json()["device_id"]
    other_room = await _make_room(client, h, "Hall", "common")
    cam2 = await _pair_camera(client, h, room_id=other_room["id"])
    assert (await _device_row(client, h, cam))["send_snapshots"] is True
    r = await client.patch(f"/rooms/{room['id']}", json={"kind": "bathroom"}, headers=h)
    assert r.status_code == 200
    assert (await _device_row(client, h, cam))["send_snapshots"] is False
    assert (await _device_row(client, h, mic))["send_snapshots"] is True
    assert (await _device_row(client, h, cam2))["send_snapshots"] is True


# 3
async def test_person_away_not_guarding_is_stored_but_not_processed(
    client, session, redis, signed_up, jobs
):
    _, h = await signed_up()
    room = await _room(client, h, "Hall")
    _, mic = await _device(client, h, "Mic", room_id=room)
    _, cam = await _device(client, h, "Cam", room_id=room, camera=True)
    r1 = await client.post("/device/events", json=_ev("glass_breaking"), headers=mic)
    assert r1.json()["alerts"] == 1
    r = await client.post("/device/events", files=_multipart("person_away"), headers=cam)
    assert r.status_code == 202 and r.json()["alerts"] == 0
    assert await session.get(Event, r.json()["event_id"]) is not None
    assert (await session.scalars(select(Snapshot))).all() == []
    assert await _incident_events(session) == []
    alerts = (await session.scalars(select(Alert).where(Alert.sound_key == "person_away"))).all()
    assert alerts == []
    # it is not in the incident window either: glass now, while guarding, finds no partner
    await _away(client, h)
    r = await client.post("/device/events", json=_ev("explosion"), headers=mic)
    assert await _incident_events(session, "incident_property_damage") == []


async def test_person_away_while_guarding_is_unchanged(client, session, signed_up):
    _, h = await signed_up()
    room = await _room(client, h, "Hall")
    _, mic = await _device(client, h, "Mic", room_id=room)
    cam_id, cam = await _device(client, h, "Cam", room_id=room, camera=True)
    await _away(client, h)
    await client.post("/device/events", json=_ev("glass_breaking"), headers=mic)
    r = await client.post("/device/events", files=_multipart("person_away"), headers=cam)
    assert r.json()["alerts"] == 1
    assert len((await session.scalars(select(Snapshot))).all()) == 2  # own + incident copy
    (inc,) = await _incident_events(session, "incident_break_in")
    assert inc.device_id == cam_id


# 13
async def test_incident_device_is_the_camera_even_when_it_arrived_first(client, session, signed_up):
    _, h = await signed_up()
    room = await _room(client, h, "Hall")
    _, mic = await _device(client, h, "Mic", room_id=room)
    cam_id, cam = await _device(client, h, "Cam", room_id=room, camera=True)
    await _away(client, h)
    await client.post("/device/events", files=_multipart("person_away"), headers=cam)
    await client.post("/device/events", json=_ev("glass_breaking"), headers=mic)
    (inc,) = await _incident_events(session, "incident_break_in")
    assert inc.device_id == cam_id


# 6
async def test_night_hours_with_tzinfo_are_422(client, signed_up):
    _, h = await signed_up()
    r = await client.patch(
        "/households/me", json={"night_start": "22:00:00+02:00", "night_end": "06:00"}, headers=h
    )
    assert r.status_code == 422
    r = await client.patch(
        "/households/me", json={"night_start": "22:00", "night_end": "06:00Z"}, headers=h
    )
    assert r.status_code == 422
    r = await client.patch(
        "/households/me", json={"night_start": "22:00", "night_end": "06:00"}, headers=h
    )
    assert r.status_code == 200


# 8
async def test_failure_before_commit_removes_new_snapshot_file(
    client, signed_up, snapshot_env, monkeypatch
):
    _, h = await signed_up()
    _, cam = await _device(client, h, "Cam", camera=True)

    async def boom(*a, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr(events_router, "process_event", boom)
    with pytest.raises(RuntimeError):
        await client.post("/device/events", files=_multipart("scream"), headers=cam)
    leftovers = (
        [f for _, _, files in os.walk(snapshot_env.snapshot_dir) for f in files]
        if os.path.isdir(snapshot_env.snapshot_dir)
        else []
    )
    assert leftovers == []


# 12
async def test_events_list_has_snapshot_flag(client, signed_up):
    _, h = await signed_up()
    _, cam = await _device(client, h, "Cam", camera=True)
    a = (await client.post("/device/events", files=_multipart("scream"), headers=cam)).json()
    b = (await client.post("/device/events", json=_ev("scream"), headers=cam)).json()
    items = {e["id"]: e for e in (await client.get("/events", headers=h)).json()}
    assert items[a["event_id"]]["has_snapshot"] is True
    assert items[b["event_id"]]["has_snapshot"] is False
