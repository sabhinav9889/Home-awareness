"""A password reset signs out every session: JWTs (token_version) and agent tokens."""

import asyncio
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from httpx_ws import WebSocketDisconnect
from sqlalchemy import func, select, update

from app.auth.models import AgentToken, Member
from app.config import get_settings
from app.households.models import Invite
from app.security import create_access_token
from tests.test_agent_tokens import _bearer, _closed_code, _mint
from tests.test_households import _token
from tests.test_live import WAIT, connect

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")

NEW_PW = "brand-new-pass-9"


def _jwt(headers) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


async def _reset(client, outbox, email="owner@example.com"):
    r = await client.post("/auth/password-reset/request", json={"email": email})
    assert r.status_code == 202
    body = {"token": _token(outbox), "new_password": NEW_PW}
    r = await client.post("/auth/password-reset/confirm", json=body)
    assert r.status_code == 200, r.text
    return r


async def test_jwt_issued_before_reset_is_401_after_over_http(client, signed_up, outbox):
    _, oh = await signed_up()
    assert (await client.get("/auth/me", headers=oh)).status_code == 200
    await _reset(client, outbox)
    assert (await client.get("/auth/me", headers=oh)).status_code == 401


async def test_jwt_issued_before_reset_closes_ws_4401(client, signed_up, outbox):
    _, oh = await signed_up()
    await _reset(client, outbox)
    assert await _closed_code(f"/ws?token={_jwt(oh)}") == 4401


async def test_reset_revokes_agent_tokens(client, signed_up, outbox, session):
    _, oh = await signed_up()
    made = await _mint(client, oh)
    await _reset(client, outbox)
    assert await _closed_code(f"/ws?token={made['token']}") == 4401
    await session.rollback()
    assert (await session.get(AgentToken, made["id"])).revoked_at is not None


async def test_new_login_works_after_reset(client, signed_up, outbox):
    await signed_up()
    await _reset(client, outbox)
    r = await client.post("/auth/login", json={"email": "owner@example.com", "password": NEW_PW})
    assert r.status_code == 200
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.get("/auth/me", headers=h)).status_code == 200
    assert (await _mint(client, h))["token"].startswith("ha_agent_")


async def test_jwt_without_tv_claim_counts_as_version_zero(client, signed_up, session):
    owner, _ = await signed_up()
    old = pyjwt.encode(
        {"sub": str(owner.id), "hid": owner.household_id, "exp": 4_102_444_800},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    assert (await client.get("/auth/me", headers=_bearer(old))).status_code == 200
    await session.execute(update(Member).where(Member.id == owner.id).values(token_version=1))
    await session.commit()
    assert (await client.get("/auth/me", headers=_bearer(old))).status_code == 401
    fresh = create_access_token(owner.id, owner.household_id, 1)
    assert (await client.get("/auth/me", headers=_bearer(fresh))).status_code == 200


async def test_reset_closes_open_sockets(client, signed_up, outbox):
    _, oh = await signed_up()
    async with connect(f"/ws?token={_jwt(oh)}") as ws:
        await _reset(client, outbox)
        with pytest.raises(WebSocketDisconnect) as e:
            await asyncio.wait_for(ws.receive_json(), WAIT)
    assert e.value.code == 4403


async def test_ws_recheck_catches_a_reset_between_auth_and_subscribe(
    client, signed_up, redis, monkeypatch, session
):
    owner, oh = await signed_up()
    real_pubsub = redis.pubsub

    def racing_pubsub():
        ps = real_pubsub()
        real_subscribe = ps.subscribe

        async def subscribe(*channels):
            await session.execute(
                update(Member).where(Member.id == owner.id).values(token_version=1)
            )
            await session.commit()
            await real_subscribe(*channels)

        ps.subscribe = subscribe
        return ps

    monkeypatch.setattr(redis, "pubsub", racing_pubsub)
    assert await _closed_code(f"/ws?token={_jwt(oh)}") == 4403
    assert await session.scalar(select(Member.token_version).where(Member.id == owner.id)) == 1


@pytest.mark.parametrize("name", ["   ", "a\x00b", "line\nbreak", "tab\there", "x" * 101, "\x7f"])
async def test_agent_token_name_rejects_blank_and_control_chars(client, signed_up, name):
    _, oh = await signed_up()
    r = await client.post("/me/agent-tokens", json={"name": name}, headers=oh)
    assert r.status_code == 422


async def test_agent_token_name_is_stripped(client, signed_up, session):
    _, oh = await signed_up()
    made = await _mint(client, oh, name="  my laptop  ")
    assert (await session.get(AgentToken, made["id"])).name == "my laptop"


async def test_pre_reset_jwt_cannot_mint_a_token_after_the_reset_commits(
    client, signed_up, session, monkeypatch
):
    owner, oh = await signed_up()
    real_scalar = session.scalar
    real_execute = session.execute
    bumped = []

    async def bump_once(stmt):
        if "FOR UPDATE" in str(stmt) and not bumped:  # the reset lands before the lock is taken
            bumped.append(1)
            # another process's commit: this session's loaded Member stays at version 0
            await real_execute(
                update(Member)
                .where(Member.id == owner.id)
                .values(token_version=1)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

    async def scalar(stmt, *a, **kw):
        await bump_once(stmt)
        return await real_scalar(stmt, *a, **kw)

    async def execute(stmt, *a, **kw):
        await bump_once(stmt)
        return await real_execute(stmt, *a, **kw)

    monkeypatch.setattr(session, "scalar", scalar)
    monkeypatch.setattr(session, "execute", execute)
    r = await client.post("/me/agent-tokens", json={"name": "late"}, headers=oh)
    assert r.status_code == 401
    monkeypatch.undo()
    assert await session.scalar(select(func.count()).select_from(AgentToken)) == 0


async def test_agent_token_name_is_stripped_before_the_length_check(client, signed_up):
    _, oh = await signed_up()
    r = await client.post("/me/agent-tokens", json={"name": f"  {'x' * 100}  "}, headers=oh)
    assert r.status_code == 201


async def test_reset_deletes_pending_invites_by_that_member(client, signed_up, outbox, session):
    owner, oh = await signed_up()
    assert (await client.post("/households/me/invites", headers=oh)).status_code == 201
    session.add(
        Invite(
            household_id=owner.household_id,
            token_hash="u" * 64,
            created_by=owner.id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            used_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await _reset(client, outbox)
    await session.rollback()
    rows = (await session.scalars(select(Invite))).all()
    assert [i.token_hash for i in rows] == ["u" * 64]


# Ruling 1b-R4: a reset never rotates the ntfy topic or unlinks Telegram (a safety app must
# never silently stop alerting the real phone); it tells the member to check them instead.
ALERT_CHANNELS_NOTE = (
    "If someone else had access to your account, regenerate your ntfy topic "
    "and check your Telegram link in Alert channels."
)


async def test_reset_confirm_says_check_alert_channels(client, signed_up, outbox):
    await signed_up()
    r = await _reset(client, outbox)
    assert r.json() == {"check_alert_channels": True}


async def test_reset_success_email_points_at_alert_channels(client, signed_up, outbox):
    await signed_up()
    await _reset(client, outbox)
    done = outbox[-1]
    assert done.to == "owner@example.com"
    assert "password was changed" in done.body.lower()
    assert ALERT_CHANNELS_NOTE in done.body
    assert "token=" not in done.body  # no usable link in a notice


async def test_reset_keeps_alert_channels(client, signed_up, outbox):
    _, oh = await signed_up()
    assert (await client.post("/me/alert-preferences/ntfy-topic", headers=oh)).status_code == 200
    before = (await client.get("/me/alert-preferences", headers=oh)).json()
    assert before["ntfy_topic"]
    await _reset(client, outbox)
    r = await client.post("/auth/login", json={"email": "owner@example.com", "password": NEW_PW})
    h = _bearer(r.json()["access_token"])
    assert (await client.get("/me/alert-preferences", headers=h)).json() == before


async def test_reset_succeeds_when_the_notice_email_fails(client, signed_up, outbox):
    await signed_up()
    r = await client.post("/auth/password-reset/request", json={"email": "owner@example.com"})
    assert r.status_code == 202
    body = {"token": _token(outbox), "new_password": NEW_PW}

    async def boom(*_a, **_k):
        raise OSError("smtp down")

    outbox.send = boom
    r = await client.post("/auth/password-reset/confirm", json=body)
    assert r.status_code == 200 and r.json() == {"check_alert_channels": True}
    r = await client.post("/auth/login", json={"email": "owner@example.com", "password": NEW_PW})
    assert r.status_code == 200
