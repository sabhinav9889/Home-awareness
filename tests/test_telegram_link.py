import json
from types import SimpleNamespace

import pytest
import respx
from sqlalchemy import select

import app.alerts.router as alerts_router
from app.rules.models import AlertPreference
from app.security import hash_token

TOKEN = "123:SECRET-TOKEN"
SECRET = "hook-secret"
TG_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
HOOK = f"/telegram/webhook/{SECRET}"
LINKED = "✅ Linked. You'll get Home Awareness alerts here."
EXPIRED = "Link expired. Create a new one in the dashboard."


pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s = SimpleNamespace(
        telegram_bot_token=TOKEN, telegram_bot_username="HomeBot", telegram_webhook_secret=SECRET
    )
    monkeypatch.setattr(alerts_router, "get_settings", lambda: s)
    return s


def _update(text, chat_id=555):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


async def _code(client, headers):
    r = await client.post("/me/telegram/link", headers=headers)
    assert r.status_code == 200, r.text
    link = r.json()["deep_link"]
    prefix = "https://t.me/HomeBot?start="
    assert link.startswith(prefix)
    return link.removeprefix(prefix)


async def _prefs(session, member):
    return await session.scalar(
        select(AlertPreference)
        .where(AlertPreference.member_id == member.id)
        .execution_options(populate_existing=True)
    )


@respx.mock
async def test_link_flow_sets_chat_and_enables_telegram(client, session, redis, signed_up):
    member, h = await signed_up()
    route = respx.post(TG_URL).respond(200, json={"ok": True})
    code = await _code(client, h)
    assert len(code) == 24
    assert await redis.get(f"tglink:{hash_token(code)}") == str(member.id)
    assert await redis.get(f"tglink:{code}") is None  # only the hash is stored
    assert 0 < await redis.ttl(f"tglink:{hash_token(code)}") <= 600

    r = await client.post(HOOK, json=_update(f"/start {code}"))
    assert r.status_code == 200
    p = await _prefs(session, member)
    assert p.telegram is True and p.telegram_chat_id == "555"
    assert json.loads(route.calls.last.request.content) == {"chat_id": "555", "text": LINKED}
    assert (await client.get("/me/alert-preferences", headers=h)).json()["telegram_linked"]

    # the code is single-use
    respx.post(TG_URL).respond(200, json={"ok": True})
    await client.post(HOOK, json=_update(f"/start {code}", chat_id=999))
    assert (await _prefs(session, member)).telegram_chat_id == "555"


@respx.mock
async def test_wrong_webhook_secret_404(client):
    route = respx.post(TG_URL).respond(200, json={"ok": True})
    r = await client.post("/telegram/webhook/nope", json=_update("/start x"))
    assert r.status_code == 404
    assert not route.called


async def test_webhook_404_when_secret_unset(client, settings):
    settings.telegram_webhook_secret = None
    assert (
        await client.post("/telegram/webhook/None", json=_update("/start x"))
    ).status_code == 404
    assert (await client.post("/telegram/webhook/", json=_update("/start x"))).status_code == 404


@respx.mock
async def test_expired_code_replies_expired_and_changes_nothing(client, session, signed_up):
    member, _ = await signed_up()
    route = respx.post(TG_URL).respond(200, json={"ok": True})
    r = await client.post(HOOK, json=_update("/start bogus"))
    assert r.status_code == 200
    assert json.loads(route.calls.last.request.content)["text"] == EXPIRED
    p = await _prefs(session, member)
    assert p is None or (p.telegram_chat_id is None and not p.telegram)


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"update_id": 1},
        {"message": {"chat": {"id": 1}}},
        {"message": {"text": "/start abc"}},
        {"message": {"chat": {"id": 1}, "text": "hello"}},
        {"message": {"chat": {"id": 1}, "text": "/start"}},
        {"message": "x"},
        {"message": {"chat": {"id": 1}, "text": 5}},
        [1, 2],
    ],
)
async def test_odd_updates_ignored_with_200(client, payload):
    route = respx.post(TG_URL).respond(200, json={"ok": True})
    assert (await client.post(HOOK, json=payload)).status_code == 200
    assert not route.called


async def test_malformed_json_is_200(client):
    r = await client.post(HOOK, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 200


@respx.mock
async def test_failed_reply_still_200_and_links(client, session, signed_up):
    member, h = await signed_up()
    respx.post(TG_URL).respond(500, json={"ok": False})
    code = await _code(client, h)
    assert (await client.post(HOOK, json=_update(f"/start {code}"))).status_code == 200
    assert (await _prefs(session, member)).telegram_chat_id == "555"


@respx.mock
async def test_relink_moves_chat_to_newest_member(client, session, signed_up):
    a, ha = await signed_up(email="a@example.com", household_name="A")
    b, hb = await signed_up(email="b@example.com", household_name="B")
    respx.post(TG_URL).respond(200, json={"ok": True})
    await client.post(HOOK, json=_update(f"/start {await _code(client, ha)}"))
    await client.post(HOOK, json=_update(f"/start {await _code(client, hb)}"))
    pa, pb = await _prefs(session, a), await _prefs(session, b)
    assert pa.telegram_chat_id is None and pa.telegram is False
    assert pb.telegram_chat_id == "555" and pb.telegram is True


async def test_link_requires_auth_and_config(client, signed_up, settings):
    assert (await client.post("/me/telegram/link")).status_code == 401
    _, h = await signed_up()
    settings.telegram_bot_username = None
    r = await client.post("/me/telegram/link", headers=h)
    assert r.status_code == 400 and r.json() == {"detail": "Telegram is not configured"}


async def test_link_rate_limited(client, signed_up):
    _, h = await signed_up()
    for _ in range(5):
        assert (await client.post("/me/telegram/link", headers=h)).status_code == 200
    assert (await client.post("/me/telegram/link", headers=h)).status_code == 429


@respx.mock
async def test_unlink_disables_telegram(client, session, signed_up):
    member, h = await signed_up()
    respx.post(TG_URL).respond(200, json={"ok": True})
    await client.post(HOOK, json=_update(f"/start {await _code(client, h)}"))
    assert (await client.delete("/me/telegram", headers=h)).status_code == 204
    p = await _prefs(session, member)
    assert p.telegram is False and p.telegram_chat_id is None
    assert (await client.delete("/me/telegram")).status_code == 401
    assert (await client.delete("/me/telegram", headers=h)).status_code == 204  # idempotent
