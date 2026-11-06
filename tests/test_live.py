import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from httpx_ws import WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from sqlalchemy import select

import app.main as app_main
from app.alerts.models import Alert
from app.auth.models import Member
from app.devices.models import Device
from app.events.models import Event
from app.live.hub import publish_to_household, publish_to_member

PW = "correct-horse-1"
WAIT = 2.0


@asynccontextmanager
async def connect(url):
    """Async WebSocket over ASGI: the app runs in this loop, sharing the test session and
    fakeredis (the `client` fixture installs the dependency overrides). Not a fixture:
    anyio's cancel scopes must be entered and exited in the same task."""
    transport = ASGIWebSocketTransport(app=app_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with aconnect_ws(url, c) as ws:
            yield ws


def _tok(headers) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


def _url(headers) -> str:
    return f"/ws?token={_tok(headers)}"


async def _recv(ws):
    return await asyncio.wait_for(ws.receive_json(), WAIT)


async def _silent(ws):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(ws.receive_json(), 0.3)


async def _add_member(client, outbox, oh, email):
    r = await client.post("/households/me/invites", headers=oh)
    token = r.json()["invite_url"].split("invite=")[1]
    r = await client.post(
        "/auth/signup", json={"email": email, "password": PW, "invite_token": token}
    )
    assert r.status_code == 201, r.text
    confirm = next(m for m in reversed(outbox) if m.to == email).body.split("token=")[1].split()[0]
    assert (await client.post("/auth/confirm", json={"token": confirm})).status_code == 204
    r = await client.post("/auth/login", json={"email": email, "password": PW})
    return r.json()["member_id"] if "member_id" in r.json() else None, {
        "Authorization": f"Bearer {r.json()['access_token']}"
    }


async def _member_id(session, email):
    return await session.scalar(select(Member.id).where(Member.email == email))


async def test_ws_rejects_bad_token_4401():
    async with connect("/ws?token=garbage") as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            await asyncio.wait_for(ws.receive_json(), WAIT)
        assert e.value.code == 4401


async def test_ws_rejects_missing_token_and_unconfirmed_member(client, session):
    from app.security import create_access_token

    r = await client.post(
        "/auth/signup",
        json={"email": "u@example.com", "password": PW, "household_name": "H"},
    )
    assert r.status_code == 201
    m = (await session.execute(select(Member).where(Member.email == "u@example.com"))).scalar_one()
    for url in ("/ws", f"/ws?token={create_access_token(m.id, m.household_id)}"):
        async with connect(url) as ws:
            with pytest.raises(WebSocketDisconnect) as e:
                await asyncio.wait_for(ws.receive_json(), WAIT)
            assert e.value.code == 4401


async def test_ws_receives_event_published_to_household(signed_up, redis):
    owner, oh = await signed_up()
    async with connect(_url(oh)) as ws:
        await publish_to_household(redis, owner.household_id, {"kind": "event", "n": 1})
        assert await _recv(ws) == {"kind": "event", "n": 1}


async def test_ws_receives_alert_only_for_own_member(client, signed_up, outbox, redis):
    owner, oh = await signed_up()
    _, mh = await _add_member(client, outbox, oh, "m@example.com")
    async with connect(_url(oh)) as ows, connect(_url(mh)) as mws:
        await publish_to_member(redis, owner.id, {"kind": "alert", "alert_id": 1})
        assert await _recv(ows) == {"kind": "alert", "alert_id": 1}
        await _silent(mws)


async def test_removed_member_socket_closed_4403(client, signed_up, outbox, redis, session):
    owner, oh = await signed_up()
    _, mh = await _add_member(client, outbox, oh, "m@example.com")
    mid = await _member_id(session, "m@example.com")
    async with connect(_url(mh)) as ws:
        r = await client.delete(f"/households/me/members/{mid}", headers=oh)
        assert r.status_code == 204
        with pytest.raises(WebSocketDisconnect) as e:
            await asyncio.wait_for(ws.receive_json(), WAIT)
        assert e.value.code == 4403
    # the server side is torn down: its subscriptions are gone, so a household event goes nowhere
    chan = f"live:household:{owner.household_id}"
    for _ in range(50):
        if (await redis.pubsub_numsub(chan))[0][1] == 0:
            break
        await asyncio.sleep(0.02)
    assert (await redis.pubsub_numsub(chan))[0][1] == 0
    assert await redis.publish(chan, "{}") == 0


async def test_other_household_receives_nothing(signed_up, redis):
    a, ah = await signed_up(email="a@example.com", household_name="A")
    _, bh = await signed_up(email="b@example.com", household_name="B")
    async with connect(_url(ah)) as aws, connect(_url(bh)) as bws:
        await publish_to_household(redis, a.household_id, {"kind": "event"})
        await publish_to_member(redis, a.id, {"kind": "alert"})
        got = {(await _recv(aws))["kind"], (await _recv(aws))["kind"]}  # positive control
        assert got == {"event", "alert"}
        await _silent(bws)


async def test_member_removed_between_auth_and_subscribe_is_closed_4403(
    client, signed_up, outbox, redis, session, monkeypatch
):
    owner, oh = await signed_up()
    _, mh = await _add_member(client, outbox, oh, "m@example.com")
    mid = await _member_id(session, "m@example.com")
    real_pubsub = redis.pubsub

    def racing_pubsub():
        ps = real_pubsub()
        real_subscribe = ps.subscribe

        async def subscribe(*channels):
            # the removal lands after auth succeeded but before we are listening
            r = await client.delete(f"/households/me/members/{mid}", headers=oh)
            assert r.status_code == 204
            await real_subscribe(*channels)

        ps.subscribe = subscribe
        return ps

    monkeypatch.setattr(redis, "pubsub", racing_pubsub)
    async with connect(_url(mh)) as ws:
        await publish_to_household(redis, owner.household_id, {"kind": "event"})
        with pytest.raises(WebSocketDisconnect) as e:
            await asyncio.wait_for(ws.receive_json(), WAIT)
        assert e.value.code == 4403


async def test_socket_close_unsubscribes(signed_up, redis):
    owner, oh = await signed_up()
    async with connect(_url(oh)) as ws:
        await publish_to_household(redis, owner.household_id, {"kind": "event"})
        await _recv(ws)
        assert (await redis.pubsub_numsub(f"live:household:{owner.household_id}"))[0][1] == 1
    for _ in range(50):
        if (await redis.pubsub_numsub(f"live:household:{owner.household_id}"))[0][1] == 0:
            break
        await asyncio.sleep(0.02)
    assert (await redis.pubsub_numsub(f"live:household:{owner.household_id}"))[0][1] == 0
    assert (await redis.pubsub_numsub(f"live:member:{owner.id}"))[0][1] == 0


async def test_publish_never_raises_on_redis_failure():
    class Broken:
        async def publish(self, *a):
            raise ConnectionError("down")

    assert await publish_to_member(Broken(), 1, {"kind": "x"}) == 0
    assert await publish_to_household(Broken(), 1, {"kind": "x"}) == 0
    from app.live.hub import publish_disconnect

    await publish_disconnect(Broken(), [1, 2])


async def _shared_event_alerts(session, members):
    first = members[0]
    device = Device(household_id=first.household_id, name="Hall", kind="pi", key_hash="h-live")
    session.add(device)
    await session.flush()
    now = datetime(2026, 11, 7, 14, 5, 30, tzinfo=UTC)
    ev = Event(
        household_id=first.household_id,
        device_id=device.id,
        sound_key="scream",
        confidence=0.9,
        occurred_at=now,
        received_at=now,
    )
    session.add(ev)
    await session.flush()
    alerts = []
    for m in members:
        a = Alert(household_id=m.household_id, member_id=m.id, event_id=ev.id, sound_key="scream")
        session.add(a)
        alerts.append(a)
    await session.flush()
    return ev, alerts


async def _two_member_setup(client, signed_up, outbox, session):
    owner, oh = await signed_up()
    _, mh = await _add_member(client, outbox, oh, "m@example.com")
    member = (
        await session.execute(select(Member).where(Member.email == "m@example.com"))
    ).scalar_one()
    ev, (oa, ma) = await _shared_event_alerts(session, [owner, member])
    return owner, oh, member, mh, ev, oa, ma


async def test_ack_marks_all_members_alerts_for_event_and_broadcasts(
    client, signed_up, outbox, session
):
    owner, oh, member, mh, ev, oa, ma = await _two_member_setup(client, signed_up, outbox, session)
    async with connect(_url(mh)) as ws:
        r = await client.post(f"/alerts/{oa.id}/ack", headers=oh)
        assert r.status_code == 200, r.text
        msg = await _recv(ws)
    assert msg["kind"] == "alert_ack"
    assert msg["event_id"] == ev.id
    assert msg["by_member_id"] == owner.id
    assert msg["by_email"] == "owner@example.com"
    assert msg["at"]
    await session.refresh(oa)
    await session.refresh(ma)
    assert oa.acknowledged_by == ma.acknowledged_by == owner.id
    assert oa.acknowledged_at is not None and ma.acknowledged_at is not None


async def test_second_ack_409_names_first_handler(client, signed_up, outbox, session):
    owner, oh, member, mh, ev, oa, ma = await _two_member_setup(client, signed_up, outbox, session)
    oa_id, ma_id = oa.id, ma.id  # the 409 path rolls back, which expires loaded instances
    assert (await client.post(f"/alerts/{oa_id}/ack", headers=oh)).status_code == 200
    r = await client.post(f"/alerts/{ma_id}/ack", headers=mh)
    assert r.status_code == 409
    assert r.json() == {"detail": "already handled", "by": "owner@example.com"}
    # the first acknowledger acking again is also "already handled"
    assert (await client.post(f"/alerts/{oa_id}/ack", headers=oh)).status_code == 409


async def test_ack_someone_elses_alert_in_same_household_404(client, signed_up, outbox, session):
    owner, oh, member, mh, ev, oa, ma = await _two_member_setup(client, signed_up, outbox, session)
    r = await client.post(f"/alerts/{oa.id}/ack", headers=mh)
    assert r.status_code == 404 and r.json() == {"detail": "not found"}


async def test_test_alert_acknowledges_only_itself(client, signed_up, session):
    owner, oh = await signed_up()
    ids = [(await client.post("/me/test-alert", headers=oh)).json()["alert_id"] for _ in range(2)]
    assert (await client.post(f"/alerts/{ids[0]}/ack", headers=oh)).status_code == 200
    rows = {a.id: a for a in (await session.scalars(select(Alert).where(Alert.id.in_(ids))))}
    await session.refresh(rows[ids[0]])
    await session.refresh(rows[ids[1]])
    assert rows[ids[0]].acknowledged_by == owner.id
    assert rows[ids[1]].acknowledged_by is None


async def test_ack_other_households_alert_404(client, two_households):
    headers_a, headers_b = two_households
    aid = (await client.post("/me/test-alert", headers=headers_a)).json()["alert_id"]
    r = await client.post(f"/alerts/{aid}/ack", headers=headers_b)
    assert r.status_code == 404 and r.json() == {"detail": "not found"}


async def test_publish_returns_live_listener_count(redis):
    assert await publish_to_member(redis, 1, {"kind": "x"}) == 0
    ps = redis.pubsub()
    await ps.subscribe("live:member:1")
    try:
        assert await publish_to_member(redis, 1, {"kind": "x"}) == 1
    finally:
        await ps.unsubscribe()
        await ps.aclose()
