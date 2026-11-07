import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import func, select

import app.snapshots.store as store
from app.alerts.sweep import sweep_snapshots
from app.config import Settings
from app.events.models import Event
from app.households.models import Household
from app.snapshots.models import Snapshot
from tests import test_delivery as td
from tests.doubles import make_jpeg
from tests.test_delivery import TG_URL, TOKEN, _alert, _prefs, _run

# Reuse test_delivery's fixtures (the autouse `settings` patches delivery's get_settings).
settings = td.settings
factory = td.factory
published = td.published
sleeps = td.sleeps

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")

JPEG = make_jpeg()


async def _camera(client, h, name="Cam", **kw):
    body = {"name": name, "kind": "camera", "capabilities": "video", "consent_confirmed": True}
    body.update(kw)
    r = await client.post("/devices/pairing-codes", json=body, headers=h)
    assert r.status_code == 201, r.text
    r = await client.post("/device/pair", json={"code": r.json()["code"]})
    assert r.status_code == 201, r.text
    return r.json()["device_id"], {"X-Device-Key": r.json()["device_key"]}


def _event_json():
    return json.dumps(
        {"type": "scream", "confidence": 0.9, "occurred_at": datetime.now(UTC).isoformat()}
    )


async def _post(client, dk, image=JPEG, *, ctype="image/jpeg"):
    files = {"event": (None, _event_json())}
    if image is not None:
        files["snapshot"] = ("snapshot.jpg", image, ctype)
    return await client.post("/device/events", files=files, headers=dk)


async def _snapshots(session):
    return (await session.execute(select(Snapshot))).scalars().all()


def _read(env, snap):
    with open(os.path.join(env.snapshot_dir, snap.path), "rb") as f:
        return f.read()


async def test_multipart_event_stores_encrypted_snapshot(client, signed_up, session, snapshot_env):
    member, h = await signed_up()
    _, dk = await _camera(client, h)
    r = await _post(client, dk)
    assert r.status_code == 202, r.text
    (snap,) = await _snapshots(session)
    assert snap.event_id == r.json()["event_id"] and snap.household_id == member.household_id
    assert snap.size_bytes == len(_read(snapshot_env, snap)) > len(JPEG) and snap.path.startswith(
        f"{member.household_id}/"
    )
    raw = _read(snapshot_env, snap)
    assert raw != JPEG and JPEG[:16] not in raw
    assert Fernet(snapshot_env.snapshot_key.encode()).decrypt(raw) == JPEG
    assert store.load(snap) == JPEG


async def test_json_event_still_works_without_snapshot(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    body = json.loads(_event_json())
    r = await client.post("/device/events", json=body, headers=dk)
    assert r.status_code == 202 and await _snapshots(session) == []
    assert (await _post(client, dk, image=None)).status_code == 202  # multipart, no image


async def test_invalid_multipart_event_part_is_422(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    r = await client.post("/device/events", files={"snapshot": ("s.jpg", JPEG)}, headers=dk)
    assert r.status_code == 422
    r = await client.post("/device/events", files={"event": (None, "{nope")}, headers=dk)
    assert r.status_code == 422
    bad = json.dumps({"type": "scream", "confidence": 9, "occurred_at": "2026-01-01T00:00:00Z"})
    r = await client.post("/device/events", files={"event": (None, bad)}, headers=dk)
    assert r.status_code == 422
    assert (await session.scalar(select(func.count()).select_from(Event))) == 0


@pytest.mark.parametrize(
    "image",
    [
        b"GIF89a" + b"\x00" * 100,  # not a JPEG
        b"\xff\xd8\xff\xe0" + b"\x00" * 100,  # truncated: no EOI marker
        make_jpeg(300 * 1024 + 1),  # over 300 KB
        b"",
    ],
    ids=["not-jpeg", "truncated", "oversize", "empty"],
)
async def test_bad_image_keeps_event_drops_image(client, signed_up, session, image):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    r = await _post(client, dk, image)
    assert r.status_code == 202, r.text
    assert await session.get(Event, r.json()["event_id"]) is not None
    assert await _snapshots(session) == []


async def test_exactly_300kb_is_kept(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    assert (await _post(client, dk, make_jpeg(300 * 1024))).status_code == 202
    assert len(await _snapshots(session)) == 1


async def test_audio_only_device_image_dropped(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _camera(client, h, capabilities="audio", kind="pi", consent_confirmed=False)
    assert (await _post(client, dk)).status_code == 202
    assert await _snapshots(session) == []


async def test_device_with_snapshots_off_drops_image(client, signed_up, session):
    _, h = await signed_up()
    device_id, dk = await _camera(client, h)
    r = await client.patch(f"/devices/{device_id}", json={"send_snapshots": False}, headers=h)
    assert r.status_code == 200, r.text
    assert (await _post(client, dk)).status_code == 202
    assert await _snapshots(session) == []


async def test_storage_error_never_fails_the_event(client, signed_up, session, monkeypatch):
    _, h = await signed_up()
    _, dk = await _camera(client, h)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_file", boom)
    r = await _post(client, dk)
    assert r.status_code == 202, r.text
    assert await session.get(Event, r.json()["event_id"]) is not None
    assert await _snapshots(session) == []


async def test_missing_key_drops_images_and_warns_once(
    client, signed_up, session, snapshot_env, caplog
):
    snapshot_env.snapshot_key = None
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    with caplog.at_level(logging.WARNING, logger="app.snapshots.store"):
        assert (await _post(client, dk)).status_code == 202
        assert (await _post(client, dk)).status_code == 202
    assert await _snapshots(session) == []
    warnings = [r for r in caplog.records if r.name == "app.snapshots.store"]
    assert len(warnings) == 1 and "SNAPSHOT_KEY" in warnings[0].getMessage()
    assert not os.path.exists(snapshot_env.snapshot_dir) or not os.listdir(
        snapshot_env.snapshot_dir
    )


def test_invalid_snapshot_key_fails_startup_without_echoing_it():
    base = {
        "database_url": "postgresql+asyncpg://x/y",
        "redis_url": "redis://x",
        "jwt_secret": "s" * 32,
    }
    with pytest.raises(ValidationError, match="SNAPSHOT_KEY") as e:
        Settings(_env_file=None, snapshot_key="not-a-fernet-key", **base)
    assert "not-a-fernet-key" not in str(e.value)
    assert Settings(_env_file=None, snapshot_key="", **base).snapshot_key is None
    assert Settings(_env_file=None, **base).snapshot_key is None
    good = Fernet.generate_key().decode()
    assert Settings(_env_file=None, snapshot_key=good, **base).snapshot_key == good
    assert Settings(_env_file=None, **base).snapshot_dir == "/data/snapshots"


# --- GET /snapshots/{event_id} ---


async def test_get_snapshot_member_agent_and_auth(client, signed_up, session, member_headers):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    event_id = (await _post(client, dk)).json()["event_id"]

    r = await client.get(f"/snapshots/{event_id}", headers=h)
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"

    other = await member_headers(h)  # another member of the same household
    assert (await client.get(f"/snapshots/{event_id}", headers=other)).status_code == 200

    token = (await client.post("/me/agent-tokens", json={"name": "laptop"}, headers=h)).json()
    agent = {"Authorization": f"Bearer {token['token']}"}
    assert (await client.get(f"/snapshots/{event_id}", headers=agent)).content == JPEG

    assert (await client.get(f"/snapshots/{event_id}")).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await client.get(f"/snapshots/{event_id}", headers=bad)).status_code == 401


async def test_get_snapshot_404s(client, signed_up, session, snapshot_env):
    _, ha = await signed_up(email="a@example.com", household_name="A")
    _, hb = await signed_up(email="b@example.com", household_name="B")
    _, dk = await _camera(client, ha)
    event_id = (await _post(client, dk)).json()["event_id"]
    plain = (await _post(client, dk, image=None)).json()["event_id"]
    assert (await client.get(f"/snapshots/{event_id}", headers=hb)).json() == {
        "detail": "not found"
    }
    assert (await client.get(f"/snapshots/{plain}", headers=ha)).status_code == 404  # no image
    assert (await client.get("/snapshots/999999", headers=ha)).status_code == 404
    # File lost from disk: a clean 404, not a 500.
    (snap,) = await _snapshots(session)
    os.remove(os.path.join(snapshot_env.snapshot_dir, snap.path))
    assert (await client.get(f"/snapshots/{event_id}", headers=ha)).status_code == 404


async def test_removed_member_cannot_fetch_snapshot(client, signed_up, session, member_headers):
    owner, h = await signed_up()
    _, dk = await _camera(client, h)
    event_id = (await _post(client, dk)).json()["event_id"]
    other = await member_headers(h, email="gone@example.com")
    members = (await client.get("/households/me", headers=h)).json()["members"]
    gone = next(m for m in members if m["email"] == "gone@example.com")
    assert (
        await client.delete(f"/households/me/members/{gone['id']}", headers=h)
    ).status_code < 300
    assert (await client.get(f"/snapshots/{event_id}", headers=other)).status_code == 401


# --- retention and cap ---


def _age(session, snap, **delta):
    snap.created_at = datetime.now(UTC) - timedelta(**delta)


async def test_sweep_deletes_snapshots_past_household_retention(
    client, signed_up, session, snapshot_env
):
    member, h = await signed_up()
    _, dk = await _camera(client, h)
    ids = [(await _post(client, dk)).json()["event_id"] for _ in range(3)]
    old, mid, new = await _snapshots(session)
    _age(session, old, days=2)
    _age(session, mid, hours=23)
    await session.flush()
    # Retention 30: nothing is due. Then 30 -> 1 day: the 2-day-old image goes on the next
    # sweep, the 23 h one stays.
    assert (
        await client.patch("/households/me", json={"snapshot_retention_days": 30}, headers=h)
    ).status_code == 200
    assert await sweep_snapshots(session, datetime.now(UTC)) == 0
    assert len(await _snapshots(session)) == 3
    assert (
        await client.patch("/households/me", json={"snapshot_retention_days": 1}, headers=h)
    ).status_code == 200
    old_path = os.path.join(snapshot_env.snapshot_dir, old.path)
    assert await sweep_snapshots(session, datetime.now(UTC)) == 1
    assert not os.path.exists(old_path)
    left = {s.event_id for s in await _snapshots(session)}
    assert left == {ids[1], ids[2]}
    # The event row survives, and so does the endpoint (404 for the missing image).
    assert await session.get(Event, ids[0]) is not None
    assert (await client.get(f"/snapshots/{ids[0]}", headers=h)).status_code == 404
    assert (await client.get(f"/snapshots/{ids[2]}", headers=h)).status_code == 200


async def test_sweep_uses_each_households_own_retention(client, signed_up, session):
    _, ha = await signed_up(email="a@example.com", household_name="A")
    _, hb = await signed_up(email="b@example.com", household_name="B")
    for h in (ha, hb):
        _, dk = await _camera(client, h)
        assert (await _post(client, dk)).status_code == 202
    await client.patch("/households/me", json={"snapshot_retention_days": 30}, headers=hb)
    for s in await _snapshots(session):
        _age(session, s, days=10)
    await session.flush()
    assert await sweep_snapshots(session, datetime.now(UTC)) == 1  # default 7 days: A's goes
    (left,) = await _snapshots(session)
    assert (await session.get(Household, left.household_id)).snapshot_retention_days == 30


async def test_cap_evicts_oldest_first(client, signed_up, session, snapshot_env, monkeypatch):
    monkeypatch.setattr(store, "CAP_BYTES", 6000)
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    ids = []
    for i in range(3):  # 3 x 2000 B > 5000 B
        ids.append((await _post(client, dk)).json()["event_id"])
        for s in await _snapshots(session):
            if s.event_id == ids[0] and i < 2:
                _age(session, s, hours=3)  # make the first clearly the oldest
        await session.flush()
    left = {s.event_id for s in await _snapshots(session)}
    assert left == {ids[1], ids[2]}
    assert (
        len(
            os.listdir(
                os.path.join(snapshot_env.snapshot_dir, str(_hid(await _snapshots(session))))
            )
        )
        == 2
    )


def _hid(snaps):
    return snaps[0].household_id


async def test_orphan_files_removed_after_a_day(client, signed_up, session, snapshot_env):
    member, h = await signed_up()
    _, dk = await _camera(client, h)
    assert (await _post(client, dk)).status_code == 202
    (snap,) = await _snapshots(session)
    hdir = os.path.join(snapshot_env.snapshot_dir, str(member.household_id))
    fresh = os.path.join(hdir, "fresh.bin")
    stale = os.path.join(hdir, "stale.bin")
    gone_hh = os.path.join(snapshot_env.snapshot_dir, "424242")
    os.makedirs(gone_hh)
    stale2 = os.path.join(gone_hh, "x.bin")
    for p in (fresh, stale, stale2):
        with open(p, "wb") as f:
            f.write(b"x")
    old = time.time() - 2 * 86400
    os.utime(stale, (old, old))
    os.utime(stale2, (old, old))
    await sweep_snapshots(session, datetime.now(UTC))
    assert os.path.exists(fresh) and not os.path.exists(stale) and not os.path.exists(stale2)
    assert os.path.exists(os.path.join(snapshot_env.snapshot_dir, snap.path))  # referenced: kept


async def test_deleting_the_event_cascades_row(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    event_id = (await _post(client, dk)).json()["event_id"]
    await session.delete(await session.get(Event, event_id))
    await session.flush()
    assert await _snapshots(session) == []


# --- live messages and delivery ---


async def test_live_event_message_has_snapshot_flag(client, signed_up, monkeypatch):
    import app.events.router as events_router

    _, h = await signed_up()
    _, dk = await _camera(client, h)
    seen = []

    async def fake(redis, household_id, msg):
        seen.append(msg)

    monkeypatch.setattr(events_router, "publish_to_household", fake)
    await _post(client, dk)
    await _post(client, dk, image=None)
    assert [m["has_snapshot"] for m in seen] == [True, False]


async def _with_snapshot(session, member, **kw):
    alert = await _alert(session, member, **kw)
    snap = await store.save(session, member.household_id, alert.event_id, JPEG)
    assert snap is not None
    return alert


@respx.mock
async def test_telegram_sendphoto_with_caption_and_ntfy_attachment(
    signed_up, session, factory, redis, published
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="my-topic")
    alert = await _with_snapshot(session, member)
    photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    msg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    ntfy = respx.put("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    plain = respx.post("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis)

    text = "⚠️ Scream detected by Hall at 14:05 UTC — Home Awareness"
    assert photo.call_count == 1 and not msg.called and not plain.called
    req = photo.calls[0].request
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert JPEG in req.content and text.encode() in req.content and b"42" in req.content
    assert ntfy.call_count == 1
    nreq = ntfy.calls[0].request
    assert nreq.content == JPEG and nreq.headers["Filename"] == "snapshot.jpg"
    assert nreq.headers["Priority"] == "urgent" and nreq.headers["Tags"] == "rotating_light"
    from email.header import decode_header

    raw, enc = decode_header(nreq.headers["Message"])[0]
    assert (raw.decode(enc) if enc else raw) == text
    assert published[0][1]["has_snapshot"] is True


@respx.mock
async def test_telegram_photo_failure_falls_back_to_text(
    signed_up, session, factory, redis, published
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42")
    alert = await _with_snapshot(session, member)
    photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "bad photo"})
    )
    msg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(alert, factory, redis)
    assert photo.call_count == 1 and msg.call_count == 1
    assert b"Scream detected" in msg.calls[0].request.content
    await session.refresh(alert)
    assert alert.status in ("sent", "partial")


@respx.mock
async def test_without_snapshot_delivery_is_unchanged(
    signed_up, session, factory, redis, published
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="my-topic")
    alert = await _alert(session, member)
    photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto")
    msg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    ntfy = respx.post("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis)
    assert not photo.called and msg.call_count == 1 and ntfy.call_count == 1
    assert published[0][1]["has_snapshot"] is False


@respx.mock
async def test_lost_file_degrades_to_text_alert(
    signed_up, session, factory, redis, published, snapshot_env
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42")
    alert = await _with_snapshot(session, member)
    (snap,) = await _snapshots(session)
    os.remove(os.path.join(snapshot_env.snapshot_dir, snap.path))
    photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto")
    msg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(alert, factory, redis)
    assert not photo.called and msg.call_count == 1
    assert published[0][1]["has_snapshot"] is False


# --- fix round ---


async def _files(env):
    out = []
    for root, _, names in os.walk(env.snapshot_dir):
        out += [os.path.join(root, n) for n in names]
    return out


async def test_enforce_cap_failure_leaves_event_without_snapshot_or_file(
    client, signed_up, session, snapshot_env, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    async def boom(*a, **k):
        raise OperationalError("SELECT", {}, Exception("db gone"))

    monkeypatch.setattr(store, "enforce_cap", boom)
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    r = await _post(client, dk)
    assert r.status_code == 202, r.text
    assert await session.get(Event, r.json()["event_id"]) is not None
    assert await _snapshots(session) == [] and await _files(snapshot_env) == []


async def test_evicted_files_deleted_only_after_commit(
    client, signed_up, session, snapshot_env, monkeypatch
):
    monkeypatch.setattr(store, "CAP_BYTES", 6000)
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    await _post(client, dk)
    (first,) = await _snapshots(session)
    first_path = os.path.join(snapshot_env.snapshot_dir, first.path)
    first.created_at = datetime.now(UTC) - timedelta(hours=1)
    await session.flush()
    await _post(client, dk)
    assert os.path.exists(first_path)  # 2 x ~2.7 KB fits
    seen = []
    real = store.delete_deferred_files

    async def spy(sess):
        seen.append(list(sess.info.get(store.DEFERRED, [])))
        await real(sess)

    monkeypatch.setattr(store, "delete_deferred_files", spy)
    await _post(client, dk)  # third overflows: oldest evicted, file removed post-commit
    assert seen[-1] == [first.path] and not os.path.exists(first_path)


async def test_save_failure_midway_rolls_back_row_and_deletes_new_file(
    client, signed_up, session, snapshot_env, monkeypatch
):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    ev = (await _post(client, dk, image=None)).json()["event_id"]
    member_hh = (await session.get(Event, ev)).household_id

    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(store, "enforce_cap", boom)
    assert await store.save(session, member_hh, ev, JPEG) is None
    assert await _snapshots(session) == [] and await _files(snapshot_env) == []


@respx.mock
async def test_telegram_timeout_on_photo_does_not_fall_back(
    signed_up, session, factory, redis, published, sleeps
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42")
    alert = await _with_snapshot(session, member)
    photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    msg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(alert, factory, redis, sleeps)
    assert photo.call_count == 3 and not msg.called  # normal retries, never the text path


@respx.mock
async def test_telegram_ok_false_falls_back_once(signed_up, session, factory, redis, published):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42")
    alert = await _with_snapshot(session, member)
    respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "no"})
    )
    msg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(alert, factory, redis)
    assert msg.call_count == 1


@respx.mock
@pytest.mark.parametrize("status", [400, 413])
async def test_ntfy_4xx_on_attachment_falls_back_to_text(
    signed_up, session, factory, redis, published, status
):
    member, _ = await signed_up()
    await _prefs(session, member, ntfy_topic="my-topic")
    alert = await _with_snapshot(session, member)
    put = respx.put("https://ntfy.test/my-topic").mock(return_value=httpx.Response(status))
    post = respx.post("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis)
    assert put.call_count == 1 and post.call_count == 1
    assert b"Scream detected" in post.calls[0].request.content
    assert post.calls[0].request.headers["Priority"] == "urgent"


@respx.mock
async def test_ntfy_transport_error_on_attachment_does_not_fall_back(
    signed_up, session, factory, redis, published, sleeps
):
    member, _ = await signed_up()
    await _prefs(session, member, ntfy_topic="my-topic")
    alert = await _with_snapshot(session, member)
    put = respx.put("https://ntfy.test/my-topic").mock(side_effect=httpx.ConnectError("down"))
    post = respx.post("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis, sleeps)
    assert put.call_count == 3 and not post.called


async def test_realistic_oversize_jpeg_is_dropped_not_refused(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    r = await _post(client, dk, make_jpeg(1_500_000))
    assert r.status_code == 202, r.text
    assert await session.get(Event, r.json()["event_id"]) is not None
    assert await _snapshots(session) == []


async def test_multipart_hard_limit_and_length_required(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    r = await _post(client, dk, make_jpeg(8 * 1024 * 1024 + 10))
    assert r.status_code == 413

    async def chunks():
        yield b"--x\r\n"

    r = await client.post(
        "/device/events",
        content=chunks(),
        headers={**dk, "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert r.status_code == 411
    assert (await session.scalar(select(func.count()).select_from(Event))) == 0


async def test_multipart_incident_key_is_422(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _camera(client, h)
    body = json.dumps(
        {
            "type": "incident_break_in",
            "confidence": 0.9,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
    )
    r = await client.post(
        "/device/events", files={"event": (None, body), "snapshot": ("s.jpg", JPEG)}, headers=dk
    )
    assert r.status_code == 422
