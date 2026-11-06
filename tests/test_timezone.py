from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.households.models import Household
from app.households.timezone import tz_label
from tests.test_delivery import TG_URL, _alert, _prefs, _run
from tests.test_delivery import factory as factory  # fixture re-exports
from tests.test_delivery import published as published
from tests.test_delivery import settings as settings
from tests.test_households import PW, _confirm_login, _invite, _join

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")


async def test_default_timezone_is_utc(client, signed_up):
    _, headers = await signed_up()
    r = await client.get("/households/me", headers=headers)
    assert r.json()["timezone"] == "UTC"


async def test_signup_with_asia_kolkata(client, outbox):
    r = await client.post(
        "/auth/signup",
        json={
            "email": "k@example.com",
            "password": PW,
            "household_name": "Kolkata",
            "timezone": "Asia/Kolkata",
        },
    )
    assert r.status_code == 201
    headers = await _confirm_login(client, outbox, "k@example.com")
    assert (await client.get("/households/me", headers=headers)).json()["timezone"] == (
        "Asia/Kolkata"
    )


@pytest.mark.parametrize("bad", ["Mars/Base", "", "+05:30"])
async def test_invalid_timezone_422(client, signed_up, bad):
    _, headers = await signed_up()
    r = await client.patch("/households/me", json={"timezone": bad}, headers=headers)
    assert r.status_code == 422
    r = await client.post(
        "/auth/signup",
        json={"email": "x@example.com", "password": PW, "household_name": "H", "timezone": bad},
    )
    assert r.status_code == 422


async def test_only_owner_changes_timezone(client, signed_up, outbox):
    _, oh = await signed_up()
    await _join(client, await _invite(client, oh))
    mh = await _confirm_login(client, outbox, "m@example.com")
    r = await client.patch("/households/me", json={"timezone": "Asia/Kolkata"}, headers=mh)
    assert r.status_code == 403
    r = await client.patch("/households/me", json={"timezone": "Asia/Kolkata"}, headers=oh)
    assert r.status_code == 200 and r.json()["timezone"] == "Asia/Kolkata"
    assert (await client.get("/households/me", headers=mh)).json()["timezone"] == "Asia/Kolkata"


def test_tz_label():
    at = datetime(2026, 11, 7, 20, 44, tzinfo=UTC)
    assert tz_label(at, "UTC") == ("20:44", "UTC")
    assert tz_label(at, "Asia/Kolkata") == ("02:14", "IST")
    assert tz_label(at, "America/Sao_Paulo") == ("17:44", "GMT-3")
    assert tz_label(at, "Asia/Kathmandu") == ("02:29", "GMT+5:45")


@respx.mock
async def test_alert_text_uses_household_local_time(signed_up, session, factory, redis, published):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic=None)
    household = await session.get(Household, member.household_id)
    household.timezone = "Asia/Kolkata"
    alert = await _alert(session, member, device_name="Hall")
    alert.created_at = datetime(2026, 11, 7, 20, 44, tzinfo=UTC)
    await session.flush()
    tg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _run(alert, factory, redis)
    import json

    text = json.loads(tg.calls[0].request.content)["text"]
    assert text == "⚠️ Scream detected by Hall at 02:14 IST — Home Awareness"
