from sqlalchemy import select

from app.devices.models import Device


async def _room(client, h, name="Hall", kind="common"):
    r = await client.post("/rooms", json={"name": name, "kind": kind}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def _code(client, h, **kw):
    body = {"name": "Cam", "kind": "camera", "capabilities": "video", "consent_confirmed": True}
    body.update(kw)
    r = await client.post("/devices/pairing-codes", json=body, headers=h)
    return r


async def _pair_camera(client, h, **kw):
    r = await _code(client, h, **kw)
    assert r.status_code == 201, r.text
    r = await client.post("/device/pair", json={"code": r.json()["code"]})
    assert r.status_code == 201, r.text
    return r.json()["device_id"]


async def _device(client, h, device_id):
    return next(d for d in (await client.get("/devices", headers=h)).json() if d["id"] == device_id)


async def test_create_and_list_rooms(client, signed_up):
    _, h = await signed_up()
    made = await _room(client, h, "Kitchen", "common")
    assert made["name"] == "Kitchen" and made["kind"] == "common"
    await _room(client, h, "Front door", "entrance")
    listed = (await client.get("/rooms", headers=h)).json()
    assert [r["name"] for r in listed] == ["Kitchen", "Front door"]
    bad = await client.post("/rooms", json={"name": "X", "kind": "attic"}, headers=h)
    assert bad.status_code == 422


async def test_member_cannot_create_rename_or_delete_rooms(client, signed_up, member_headers):
    _, h = await signed_up()
    room = await _room(client, h)
    mh = await member_headers(h)
    assert (await client.get("/rooms", headers=mh)).status_code == 200
    r = await client.post("/rooms", json={"name": "A", "kind": "other"}, headers=mh)
    assert r.status_code == 403
    r = await client.patch(f"/rooms/{room['id']}", json={"name": "B"}, headers=mh)
    assert r.status_code == 403
    assert (await client.delete(f"/rooms/{room['id']}", headers=mh)).status_code == 403


async def test_duplicate_room_name_409(client, signed_up):
    _, h = await signed_up()
    a = await _room(client, h, "Hall")
    b = await _room(client, h, "Den")
    r = await client.post("/rooms", json={"name": "Hall", "kind": "common"}, headers=h)
    assert r.status_code == 409
    r = await client.patch(f"/rooms/{b['id']}", json={"name": "Hall"}, headers=h)
    assert r.status_code == 409
    r = await client.patch(f"/rooms/{a['id']}", json={"name": "Hall", "kind": "outdoor"}, headers=h)
    assert r.status_code == 200 and r.json()["kind"] == "outdoor"


async def test_delete_room_nulls_device_room(client, signed_up):
    _, h = await signed_up()
    room = await _room(client, h, "Den")
    dev = await _pair_camera(client, h, room_id=room["id"])
    d = await _device(client, h, dev)
    assert d["room_id"] == room["id"] and d["room_name"] == "Den"
    assert (await client.delete(f"/rooms/{room['id']}", headers=h)).status_code == 204
    d = await _device(client, h, dev)
    assert d["room_id"] is None and d["room_name"] is None


async def test_video_pairing_requires_consent(client, signed_up):
    _, h = await signed_up()
    r = await _code(client, h, consent_confirmed=False)
    assert r.status_code == 422 and "confirm everyone on camera knows" in r.text
    r = await _code(client, h, capabilities="audio,video", consent_confirmed=False)
    assert r.status_code == 422
    r = await _code(client, h, capabilities="audio", consent_confirmed=False)
    assert r.status_code == 201
    r = await _code(client, h, capabilities="loud")
    assert r.status_code == 422


async def test_camera_device_fields_and_consent_recorded(client, signed_up):
    member, h = await signed_up()
    dev = await _pair_camera(client, h, capabilities="audio,video")
    d = await _device(client, h, dev)
    assert d["kind"] == "camera" and d["capabilities"] == "audio,video"
    assert d["send_snapshots"] is True and d["consent_confirmed_at"] is not None
    assert d["room_id"] is None


async def test_plain_pi_device_defaults_backward_compatible(client, signed_up):
    _, h = await signed_up()
    r = await client.post("/devices/pairing-codes", json={"name": "Pi", "kind": "pi"}, headers=h)
    assert r.status_code == 201
    code = r.json()["code"]
    dev = (await client.post("/device/pair", json={"code": code})).json()["device_id"]
    d = await _device(client, h, dev)
    assert d["capabilities"] == "audio" and d["consent_confirmed_at"] is None
    assert d["send_snapshots"] is True


async def test_bedroom_and_bathroom_cameras_default_snapshots_off(client, signed_up):
    _, h = await signed_up()
    bed = await _room(client, h, "Bed", "bedroom")
    bath = await _room(client, h, "Bath", "bathroom")
    hall = await _room(client, h, "Hall", "common")
    for room, expected in ((bed, False), (bath, False), (hall, True)):
        dev = await _pair_camera(client, h, room_id=room["id"])
        assert (await _device(client, h, dev))["send_snapshots"] is expected


async def test_pairing_code_with_other_household_room_404(client, two_households):
    ha, hb = two_households
    room = await _room(client, ha, "Hall")
    r = await _code(client, hb, room_id=room["id"])
    assert r.status_code == 404


async def test_patch_device_name_room_and_snapshots(client, signed_up, member_headers):
    _, h = await signed_up()
    room = await _room(client, h, "Hall")
    dev = await _pair_camera(client, h)
    r = await client.patch(
        f"/devices/{dev}", json={"name": "Porch", "room_id": room["id"]}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Porch" and r.json()["room_name"] == "Hall"
    r = await client.patch(f"/devices/{dev}", json={"room_id": None}, headers=h)
    assert r.json()["room_id"] is None
    r = await client.patch(f"/devices/{dev}", json={"send_snapshots": False}, headers=h)
    assert r.status_code == 200 and r.json()["send_snapshots"] is False

    mh = await member_headers(h)
    r = await client.patch(
        f"/devices/{dev}", json={"name": "Door", "room_id": room["id"]}, headers=mh
    )
    assert r.status_code == 200 and r.json()["name"] == "Door"
    r = await client.patch(f"/devices/{dev}", json={"send_snapshots": True}, headers=mh)
    assert r.status_code == 403


async def test_patch_device_rejects_foreign_room_and_bad_consent(client, two_households):
    ha, hb = two_households
    room_b = await _room(client, hb, "Hall")
    dev = await _pair_camera(client, ha)
    r = await client.patch(f"/devices/{dev}", json={"room_id": room_b["id"]}, headers=ha)
    assert r.status_code == 404
    r = await client.patch(f"/devices/{dev}", json={"consent_confirmed": False}, headers=ha)
    assert r.status_code == 422


async def test_patch_consent_records_now_and_caller(client, signed_up, session, member_headers):
    owner, h = await signed_up()
    r = await client.post("/devices/pairing-codes", json={"name": "Pi", "kind": "pi"}, headers=h)
    dev = (await client.post("/device/pair", json={"code": r.json()["code"]})).json()["device_id"]
    mh = await member_headers(h)
    r = await client.patch(f"/devices/{dev}", json={"consent_confirmed": True}, headers=mh)
    assert r.status_code == 200 and r.json()["consent_confirmed_at"] is not None
    device = (await session.scalars(select(Device).where(Device.id == dev))).one()
    assert device.consent_confirmed_by is not None and device.consent_confirmed_by != owner.id
    stamp = r.json()["consent_confirmed_at"]
    # Idempotent: a second confirmation does not move the record.
    r = await client.patch(f"/devices/{dev}", json={"consent_confirmed": True}, headers=h)
    assert r.json()["consent_confirmed_at"] == stamp
