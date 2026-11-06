"""Long-lived, revocable agent tokens for `ha-agent` (WS + ack only)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx_ws import WebSocketDisconnect
from sqlalchemy import select, update

from app.auth.models import AgentToken, Member
from app.live.hub import publish_to_member
from app.security import hash_token
from tests.test_live import WAIT, _add_member, _recv, _shared_event_alerts, connect

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")


async def _mint(client, headers, name="laptop agent"):
    r = await client.post("/me/agent-tokens", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _closed_code(url: str) -> int:
    async with connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            await asyncio.wait_for(ws.receive_json(), WAIT)
    return e.value.code


async def test_create_returns_token_once_and_stores_only_its_hash(client, signed_up, session):
    owner, oh = await signed_up()
    made = await _mint(client, oh)
    assert set(made) == {"id", "token"}
    assert made["token"].startswith("ha_agent_") and len(made["token"]) > 40
    row = await session.get(AgentToken, made["id"])
    assert row.token_hash == hash_token(made["token"])
    assert row.member_id == owner.id and row.household_id == owner.household_id
    assert row.name == "laptop agent" and row.revoked_at is None and row.last_used_at is None

    r = await client.get("/me/agent-tokens", headers=oh)
    assert r.status_code == 200
    (listed,) = r.json()
    assert listed["id"] == made["id"] and listed["name"] == "laptop agent"
    assert "token" not in listed and "token_hash" not in listed
    assert made["token"] not in r.text


async def test_revoke_hides_it_and_is_404_for_another_member(client, signed_up, outbox):
    _, oh = await signed_up()
    _, mh = await _add_member(client, outbox, oh, "m@example.com")
    made = await _mint(client, oh)
    # same household, different member: indistinguishable from missing
    r = await client.delete(f"/me/agent-tokens/{made['id']}", headers=mh)
    assert r.status_code == 404 and r.json() == {"detail": "not found"}
    assert (await client.delete(f"/me/agent-tokens/{made['id']}", headers=oh)).status_code == 204
    assert (await client.get("/me/agent-tokens", headers=oh)).json() == []


async def test_agent_token_is_rejected_on_ordinary_routes(client, signed_up):
    _, oh = await signed_up()
    agent = _bearer((await _mint(client, oh))["token"])
    for method, path in (
        ("GET", "/households/me"),
        ("GET", "/auth/me"),
        ("GET", "/me/agent-tokens"),
        ("POST", "/me/agent-tokens"),
        ("GET", "/alerts"),
    ):
        r = await client.request(method, path, headers=agent, json={"name": "x"})
        assert r.status_code == 401, (method, path, r.text)


async def test_unknown_agent_token_is_401(client):
    r = await client.post("/alerts/1/ack", headers=_bearer("ha_agent_nope"))
    assert r.status_code == 401


async def test_agent_token_acks_own_alert(client, signed_up, session):
    owner, oh = await signed_up()
    _, (alert,) = await _shared_event_alerts(session, [owner])
    agent = _bearer((await _mint(client, oh))["token"])
    r = await client.post(f"/alerts/{alert.id}/ack", headers=agent)
    assert r.status_code == 200, r.text
    assert r.json()["acknowledged_by"] == owner.id


async def test_agent_token_opens_the_live_socket(client, signed_up, redis):
    owner, oh = await signed_up()
    token = (await _mint(client, oh))["token"]
    async with connect(f"/ws?token={token}") as ws:
        await publish_to_member(redis, owner.id, {"kind": "alert", "alert_id": 1})
        assert await _recv(ws) == {"kind": "alert", "alert_id": 1}


async def test_revoked_token_closes_4401_and_cannot_ack(client, signed_up, session):
    owner, oh = await signed_up()
    _, (alert,) = await _shared_event_alerts(session, [owner])
    made = await _mint(client, oh)
    assert (await client.delete(f"/me/agent-tokens/{made['id']}", headers=oh)).status_code == 204
    assert await _closed_code(f"/ws?token={made['token']}") == 4401
    r = await client.post(f"/alerts/{alert.id}/ack", headers=_bearer(made["token"]))
    assert r.status_code == 401


async def test_removed_member_token_closes_4401(client, signed_up, outbox, session):
    _, oh = await signed_up()
    _, mh = await _add_member(client, outbox, oh, "m@example.com")
    token = (await _mint(client, mh))["token"]
    mid = await session.scalar(select(Member.id).where(Member.email == "m@example.com"))
    assert (await client.delete(f"/households/me/members/{mid}", headers=oh)).status_code == 204
    assert await _closed_code(f"/ws?token={token}") == 4401


async def test_token_revoked_between_auth_and_subscribe_is_closed(
    client, signed_up, redis, monkeypatch
):
    _, oh = await signed_up()
    made = await _mint(client, oh)
    real_pubsub = redis.pubsub

    def racing_pubsub():
        ps = real_pubsub()
        real_subscribe = ps.subscribe

        async def subscribe(*channels):
            r = await client.delete(f"/me/agent-tokens/{made['id']}", headers=oh)
            assert r.status_code == 204
            await real_subscribe(*channels)

        ps.subscribe = subscribe
        return ps

    monkeypatch.setattr(redis, "pubsub", racing_pubsub)
    assert await _closed_code(f"/ws?token={made['token']}") == 4403


async def test_last_used_at_is_written_at_most_once_a_minute(client, signed_up, session):
    owner, oh = await signed_up()
    _, (alert,) = await _shared_event_alerts(session, [owner])
    made = await _mint(client, oh)
    agent = _bearer(made["token"])
    await client.post(f"/alerts/{alert.id}/ack", headers=agent)
    row = await session.get(AgentToken, made["id"])
    await session.refresh(row)
    first = row.last_used_at
    assert first is not None and datetime.now(UTC) - first < timedelta(seconds=30)

    await client.post(f"/alerts/{alert.id}/ack", headers=agent)  # 409, still authenticated
    await session.refresh(row)
    assert row.last_used_at == first  # within the minute: no write

    stale = datetime.now(UTC) - timedelta(minutes=5)
    await session.execute(
        update(AgentToken).where(AgentToken.id == made["id"]).values(last_used_at=stale)
    )
    async with connect(f"/ws?token={made['token']}"):
        pass
    await session.refresh(row)
    assert row.last_used_at > stale + timedelta(minutes=4)


async def test_ten_active_tokens_per_member(client, signed_up, redis):
    _, oh = await signed_up()
    made = [await _mint(client, oh, f"t{i}") for i in range(10)]
    await redis.flushall()  # reset the 10/min rate-limit window: test only the cap here
    r = await client.post("/me/agent-tokens", json={"name": "eleventh"}, headers=oh)
    assert r.status_code == 409
    # revoking one frees a slot
    assert (await client.delete(f"/me/agent-tokens/{made[0]['id']}", headers=oh)).status_code == 204
    await _mint(client, oh, "eleventh")


async def test_create_is_rate_limited_per_member(client, signed_up):
    _, oh = await signed_up()
    for i in range(10):
        r = await client.post("/me/agent-tokens", json={"name": f"t{i}"}, headers=oh)
        assert r.status_code in (201, 409)
    r = await client.post("/me/agent-tokens", json={"name": "again"}, headers=oh)
    assert r.status_code == 429


@pytest.mark.parametrize("name", ["", "x" * 101])
async def test_name_is_validated(client, signed_up, name):
    _, oh = await signed_up()
    r = await client.post("/me/agent-tokens", json={"name": name}, headers=oh)
    assert r.status_code == 422
