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


async def test_wrong_password_401_and_rate_limited(client, outbox, frozen_ratelimit_clock):
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


async def test_resend_confirmation_issues_fresh_token_and_retires_old(client, outbox):
    await _signup(client)
    old = _token(outbox)
    r = await client.post("/auth/confirm/resend", json={"email": " A@x.com "})
    assert r.status_code == 202
    assert len(outbox) == 2 and outbox[1].to == "a@x.com"
    new = _token(outbox, 1)
    assert new != old
    assert (await client.post("/auth/confirm", json={"token": old})).status_code == 400
    assert (await client.post("/auth/confirm", json={"token": new})).status_code == 204


async def test_resend_is_uniform_for_unknown_and_confirmed(client, outbox):
    await _signup(client)
    await client.post("/auth/confirm", json={"token": _token(outbox)})
    sent = len(outbox)
    for email in ("a@x.com", "nobody@x.com"):
        r = await client.post("/auth/confirm/resend", json={"email": email})
        assert r.status_code == 202
        assert r.json() == {"detail": "if the address awaits confirmation, an email was sent"}
    assert len(outbox) == sent


async def test_resend_rate_limited_per_ip(client, outbox, frozen_ratelimit_clock):
    for _ in range(10):
        r = await client.post("/auth/confirm/resend", json={"email": "n@x.com"})
        assert r.status_code == 202
    r = await client.post("/auth/confirm/resend", json={"email": "n@x.com"})
    assert r.status_code == 429


async def test_signup_survives_failing_email_and_resend_recovers(client, outbox, caplog):
    import app.main as app_main
    from app.email import get_email_sender

    class Broken:
        async def send(self, to, subject, body):
            raise ConnectionError("smtp down")

    app_main.app.dependency_overrides[get_email_sender] = lambda: Broken()
    r = await _signup(client)
    assert r.status_code == 201
    assert "confirmation email" in caplog.text and "smtp down" in caplog.text
    app_main.app.dependency_overrides[get_email_sender] = lambda: outbox
    assert (await client.post("/auth/confirm/resend", json={"email": "a@x.com"})).status_code == 202
    assert (await client.post("/auth/confirm", json={"token": _token(outbox)})).status_code == 204
