from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.auth.models import EmailToken, Member
from app.households.models import Household

PW = "correct-horse-1"


async def _signup(client, email="a@x.com", name="Casa"):
    return await client.post(
        "/auth/signup", json={"email": email, "password": PW, "household_name": name}
    )


def _token(outbox, i=0):
    return outbox[i].body.split("token=")[1].split()[0]


async def test_signup_creates_owner_and_household(client, session, outbox):
    r = await _signup(client)
    assert r.status_code == 201
    member = (await session.execute(select(Member))).scalar_one()
    assert member.role == "owner"
    assert member.id == r.json()["member_id"]
    hh = await session.get(Household, r.json()["household_id"])
    assert hh.name == "Casa"
    assert "/confirm?token=" in outbox[0].body


async def test_login_requires_confirmed_email(client, outbox):
    await _signup(client)
    body = {"email": "a@x.com", "password": PW}
    r = await client.post("/auth/login", json=body)
    assert r.status_code == 403
    assert r.json() == {"detail": "email not confirmed"}
    assert (await client.post("/auth/confirm", json={"token": _token(outbox)})).status_code == 204
    r = await client.post("/auth/login", json=body)
    assert r.status_code == 200
    assert r.json()["access_token"]
    assert r.json()["token_type"] == "bearer"


async def test_email_is_case_and_space_insensitive(client, outbox):
    assert (await _signup(client, "A@x.com")).status_code == 201
    assert (await _signup(client, "a@x.com ")).status_code == 409
    await client.post("/auth/confirm", json={"token": _token(outbox)})
    r = await client.post("/auth/login", json={"email": "A@X.COM", "password": PW})
    assert r.status_code == 200


async def test_confirm_token_single_use_and_expiry(client, session, outbox):
    await _signup(client)
    token = _token(outbox)
    assert (await client.post("/auth/confirm", json={"token": token})).status_code == 204
    assert (await client.post("/auth/confirm", json={"token": token})).status_code == 400

    await _signup(client, "b@x.com")
    token2 = _token(outbox, 1)
    await session.execute(
        update(EmailToken)
        .where(EmailToken.used_at.is_(None))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert (await client.post("/auth/confirm", json={"token": token2})).status_code == 400


async def test_wrong_password_401_and_rate_limited(client, outbox, monkeypatch):
    monkeypatch.setattr("app.ratelimit.time.time", lambda: 1_800_000_000.0)
    await _signup(client)
    body = {"email": "a@x.com", "password": "wrong-password-1"}
    for _ in range(10):
        assert (await client.post("/auth/login", json=body)).status_code == 401
    assert (await client.post("/auth/login", json=body)).status_code == 429


async def test_me_requires_valid_token(client, signed_up):
    assert (await client.get("/auth/me")).status_code == 401
    member, headers = await signed_up()
    r = await client.get("/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "id": member.id,
        "email": "owner@example.com",
        "role": "owner",
        "household_id": member.household_id,
    }
    bad = {"Authorization": headers["Authorization"][:-2] + "xx"}
    assert (await client.get("/auth/me", headers=bad)).status_code == 401
