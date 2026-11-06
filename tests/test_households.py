from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.auth.models import EmailToken, Member
from app.households.models import Household, Invite

PW = "correct-horse-1"


def _token(outbox, i=-1):
    return outbox[i].body.split("token=")[1].split()[0]


async def test_reset_request_is_202_for_unknown_email_and_sends_nothing(client, outbox):
    r = await client.post("/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert r.status_code == 202
    assert outbox == []


async def test_reset_changes_password_and_token_single_use(client, signed_up, outbox):
    await signed_up(email="o@example.com")
    n = len(outbox)
    r = await client.post("/auth/password-reset/request", json={"email": " O@Example.com "})
    assert r.status_code == 202
    assert len(outbox) == n + 1
    token = _token(outbox)
    body = {"token": token, "new_password": "brand-new-pass-9"}
    assert (await client.post("/auth/password-reset/confirm", json=body)).status_code == 204
    old = await client.post("/auth/login", json={"email": "o@example.com", "password": PW})
    assert old.status_code == 401
    new = await client.post(
        "/auth/login", json={"email": "o@example.com", "password": "brand-new-pass-9"}
    )
    assert new.status_code == 200
    assert (await client.post("/auth/password-reset/confirm", json=body)).status_code == 400


async def test_reset_token_expires_and_confirm_token_is_not_a_reset_token(
    client, signed_up, session, outbox
):
    await signed_up(email="o@example.com")
    confirm_token = outbox[0].body.split("token=")[1].split()[0]
    body = {"token": confirm_token, "new_password": "brand-new-pass-9"}
    assert (await client.post("/auth/password-reset/confirm", json=body)).status_code == 400
    await client.post("/auth/password-reset/request", json={"email": "o@example.com"})
    await session.execute(
        update(EmailToken)
        .where(EmailToken.purpose == "reset")
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    body["token"] = _token(outbox)
    assert (await client.post("/auth/password-reset/confirm", json=body)).status_code == 400


async def _invite(client, headers) -> str:
    r = await client.post("/households/me/invites", headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["invite_url"].split("invite=")[1]


async def _join(client, token, email="m@example.com"):
    return await client.post(
        "/auth/signup", json={"email": email, "password": PW, "invite_token": token}
    )


async def test_invite_signup_joins_existing_household_as_member(client, signed_up, session):
    owner, headers = await signed_up()
    token = await _invite(client, headers)
    r = await _join(client, token)
    assert r.status_code == 201
    assert r.json()["household_id"] == owner.household_id
    member = (
        await session.execute(select(Member).where(Member.email == "m@example.com"))
    ).scalar_one()
    assert member.role == "member"
    assert member.household_id == owner.household_id
    assert (await session.scalar(select(Household.id).where(Household.name == "Home"))) is not None
    me = (await client.get("/households/me", headers=headers)).json()
    assert {m["email"]: m["role"] for m in me["members"]} == {
        "owner@example.com": "owner",
        "m@example.com": "member",
    }


async def test_signup_needs_exactly_one_of_household_name_or_invite(client, signed_up):
    _, headers = await signed_up()
    token = await _invite(client, headers)
    both = {"email": "x@example.com", "password": PW, "household_name": "H", "invite_token": token}
    neither = {"email": "x@example.com", "password": PW}
    assert (await client.post("/auth/signup", json=both)).status_code == 422
    assert (await client.post("/auth/signup", json=neither)).status_code == 422


async def test_expired_or_used_invite_rejected(client, signed_up, session):
    _, headers = await signed_up()
    used = await _invite(client, headers)
    assert (await _join(client, used)).status_code == 201
    assert (await _join(client, used, email="m2@example.com")).status_code == 400
    expired = await _invite(client, headers)
    await session.execute(
        update(Invite).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert (await _join(client, expired, email="m3@example.com")).status_code == 400
    assert (await _join(client, "iv_bogus", email="m4@example.com")).status_code == 400


async def test_only_owner_invites_and_removes(client, signed_up, outbox):
    owner, oh = await signed_up()
    token = await _invite(client, oh)
    await _join(client, token)
    member_headers = await _confirm_login(client, outbox, "m@example.com")
    assert (await client.post("/households/me/invites", headers=member_headers)).status_code == 403
    r = await client.delete(f"/households/me/members/{owner.id}", headers=member_headers)
    assert r.status_code == 403
    assert (await client.delete("/households/me", headers=member_headers)).status_code == 403


async def _confirm_login(client, outbox, email):
    token = next(m for m in reversed(outbox) if m.to == email).body.split("token=")[1].split()[0]
    assert (await client.post("/auth/confirm", json={"token": token})).status_code == 204
    r = await client.post("/auth/login", json={"email": email, "password": PW})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_owner_cannot_leave_or_remove_self(client, signed_up):
    owner, headers = await signed_up()
    assert (await client.post("/households/me/leave", headers=headers)).status_code == 400
    r = await client.delete(f"/households/me/members/{owner.id}", headers=headers)
    assert r.status_code == 400


async def test_owner_removes_member_and_member_can_leave(client, signed_up, outbox):
    owner, oh = await signed_up()
    for email in ("m1@example.com", "m2@example.com"):
        await _join(client, await _invite(client, oh), email=email)
    h1 = await _confirm_login(client, outbox, "m1@example.com")
    h2 = await _confirm_login(client, outbox, "m2@example.com")
    ids = {
        m["email"]: m["id"]
        for m in (await client.get("/households/me", headers=oh)).json()["members"]
    }
    assert (
        await client.delete(f"/households/me/members/{ids['m1@example.com']}", headers=oh)
    ).status_code == 204
    assert (await client.get("/households/me", headers=h1)).status_code == 401
    assert (await client.post("/households/me/leave", headers=h2)).status_code == 204
    assert (await client.get("/households/me", headers=h2)).status_code == 401
    me = (await client.get("/households/me", headers=oh)).json()
    assert [m["email"] for m in me["members"]] == ["owner@example.com"]


async def test_delete_household_removes_members_and_rows(client, signed_up, session):
    owner, oh = await signed_up()
    await _join(client, await _invite(client, oh))
    hid = owner.household_id
    assert (await client.delete("/households/me", headers=oh)).status_code == 204
    assert (await client.get("/auth/me", headers=oh)).status_code == 401
    session.expunge_all()
    assert await session.get(Household, hid) is None
    left = (await session.execute(select(Member).where(Member.household_id == hid))).all()
    assert left == []
    invites = (await session.execute(select(Invite).where(Invite.household_id == hid))).all()
    assert invites == []
