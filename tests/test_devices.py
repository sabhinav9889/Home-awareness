from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.devices.models import Device, PairingCode


async def _code(client, headers, name="Kitchen", kind="pi"):
    r = await client.post(
        "/devices/pairing-codes", json={"name": name, "kind": kind}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _pair(client, headers, **kw):
    code = (await _code(client, headers, **kw))["code"]
    r = await client.post("/device/pair", json={"code": code})
    assert r.status_code == 201, r.text
    return r.json()


async def test_pair_with_code_returns_key_once(client, signed_up, session):
    _, h = await signed_up()
    made = await _code(client, h)
    assert len(made["code"]) == 8 and made["expires_at"]
    r = await client.post("/device/pair", json={"code": made["code"]})
    assert r.status_code == 201
    body = r.json()
    assert body["device_key"].startswith("ha_dev_") and body["device_id"]
    # Key is shown once and never stored in clear text.
    listed = (await client.get("/devices", headers=h)).json()
    assert "device_key" not in listed[0] and body["device_key"] not in str(listed)
    from app.security import hash_token

    assert (
        await session.scalar(
            select(func.count())
            .select_from(Device)
            .where(Device.key_hash == hash_token(body["device_key"]))
        )
        == 1
    )
    hashes = (await session.execute(select(PairingCode.code_hash))).scalars().all()
    assert made["code"] not in hashes and hash_token(made["code"]) in hashes


async def test_pairing_code_single_use_expiry_and_case_insensitive(client, signed_up, session):
    _, h = await signed_up()
    code = (await _code(client, h))["code"]
    assert (await client.post("/device/pair", json={"code": code.lower()})).status_code == 201
    r = await client.post("/device/pair", json={"code": code})
    assert r.status_code == 400
    # expired
    code2 = (await _code(client, h))["code"]
    await session.execute(
        update(PairingCode).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    r2 = await client.post("/device/pair", json={"code": code2})
    assert r2.status_code == 400 and r2.json() == r.json()
    assert (await client.post("/device/pair", json={"code": "ZZZZZZZZ"})).json() == r.json()


async def test_wrong_code_rate_limited(client):
    for _ in range(10):
        assert (await client.post("/device/pair", json={"code": "AAAAAAAA"})).status_code == 400
    assert (await client.post("/device/pair", json={"code": "AAAAAAAA"})).status_code == 429


async def test_device_key_authenticates_and_unknown_key_401(client, signed_up):
    member, h = await signed_up()
    paired = await _pair(client, h)
    r = await client.get("/device/whoami", headers={"X-Device-Key": paired["device_key"]})
    assert r.status_code == 200
    assert r.json() == {"device_id": paired["device_id"], "household_id": member.household_id}
    assert (
        await client.get("/device/whoami", headers={"X-Device-Key": "ha_dev_nope"})
    ).status_code == 401
    assert (await client.get("/device/whoami")).status_code == 401


async def test_revoked_device_key_401_immediately(client, signed_up):
    _, h = await signed_up()
    paired = await _pair(client, h)
    key = {"X-Device-Key": paired["device_key"]}
    assert (await client.get("/device/whoami", headers=key)).status_code == 200
    assert (await client.delete(f"/devices/{paired['device_id']}", headers=h)).status_code == 204
    assert (await client.get("/device/whoami", headers=key)).status_code == 401


async def test_list_devices_scoped(client, two_households):
    ha, hb = two_households
    a = await _pair(client, ha, name="A-dev")
    await _pair(client, hb, name="B-dev", kind="phone")
    rows = (await client.get("/devices", headers=ha)).json()
    assert [(r["id"], r["name"], r["kind"], r["status"], r["last_seen_at"]) for r in rows] == [
        (a["device_id"], "A-dev", "pi", "pending", None)
    ]
