"""Sensors, alert channels and household views: real browser, real API."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from playwright.sync_api import expect

from app.config import get_settings
from tests.web.test_alert_ui import _deliver, _page, _wait_live
from tests.web.test_overview import PW, _device, _event, _member, _row, _synced

pytestmark = pytest.mark.web
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


# --- helpers ---


def _open(page, member, hash_):
    page.goto("/app/#/login")
    page.evaluate("t => sessionStorage.setItem('ha.token', t)", member.token)
    page.goto(f"/app/{hash_}")
    expect(page.locator("main [data-ready='true']")).to_have_count(1)


def _get(live, member, path):
    r = httpx.get(f"{live.base_url}{path}", headers=member.headers)
    assert r.status_code == 200, r.text
    return r.json()


def _dialog(page):
    return page.get_by_role("dialog")


def _invite_token(live, owner):
    r = httpx.post(f"{live.base_url}/households/me/invites", headers=owner.headers)
    assert r.status_code == 201, r.text
    return r.json()["invite_url"].split("invite=")[1]


def _no_overflow(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# --- sensors ---


def test_add_sensor_shows_an_8_char_code_and_a_live_countdown(page, live):
    m = _member(live)
    _open(page, m, "#/sensors")
    expect(page.locator(".pair-result")).to_be_hidden()  # no empty box before a code exists
    page.get_by_label("Sensor name").fill("Kitchen")
    page.get_by_label("Kind").select_option("pi")
    with page.expect_response(re.compile(r"/devices/pairing-codes$")) as info:
        page.get_by_role("button", name="Create pairing code").click()
    assert info.value.status == 201
    code_box = page.locator(".pair-code")
    expect(code_box).to_have_text(re.compile(rf"^[{ALPHABET}]{{8}}$"))
    code = code_box.inner_text()
    assert code_box.get_attribute("aria-label") == "Pairing code: " + " ".join(code)
    expiry = page.locator(".pair-expiry")
    expect(expiry).to_have_text(re.compile(r"Expires in (10:00|9:[0-5]\d)"))
    first = expiry.inner_text()
    page.wait_for_function(
        "t => document.querySelector('.pair-expiry').textContent !== t", arg=first, timeout=3000
    )
    # the code really pairs, as the requested kind
    r = httpx.post(f"{live.base_url}/device/pair", json={"code": code})
    assert r.status_code == 201, r.text
    [d] = _get(live, m, "/devices")
    assert (d["name"], d["kind"]) == ("Kitchen", "pi")


def test_sensor_list_and_remove_with_confirm(page, live):
    m = _member(live)
    _device(live, m, name="Hall")
    _open(page, m, "#/sensors")
    row = page.locator(".device-row").filter(has_text="Hall")
    expect(row).to_contain_text("Raspberry Pi")
    expect(row).to_contain_text("Waiting for its first heartbeat")

    row.get_by_role("button", name="Remove Hall").click()
    expect(_dialog(page)).to_be_visible()
    _dialog(page).get_by_role("button", name="Cancel").click()
    expect(_dialog(page)).to_have_count(0)
    assert len(_get(live, m, "/devices")) == 1

    row.get_by_role("button", name="Remove Hall").click()
    with page.expect_response(lambda r: r.request.method == "DELETE") as info:
        _dialog(page).get_by_role("button", name="Remove sensor").click()
    assert info.value.status == 204
    expect(page.locator(".device-row")).to_have_count(0)
    assert _get(live, m, "/devices") == []


def test_sensor_list_refreshes_on_device_online(page, live):
    m = _member(live)
    hall = _device(live, m, name="Hall")
    _open(page, m, "#/sensors")
    _wait_live(page)
    row = page.locator(".device-row").filter(has_text="Hall")
    expect(row).to_contain_text("Waiting for its first heartbeat")
    from tests.web.test_overview import _go_offline, _heartbeat

    _heartbeat(live, hall)
    _go_offline(live, hall.id)  # publishes device_offline
    expect(row).to_contain_text("Offline")
    _heartbeat(live, hall)  # back: publishes device_online
    expect(row).to_contain_text("Listening")


# --- alert channels ---


def test_popup_switch_puts_the_full_body(page, live):
    m = _member(live)
    _open(page, m, "#/channels")
    sw = page.get_by_role("switch", name="Alarm sound")
    expect(sw).to_have_attribute("aria-checked", "false")
    with page.expect_request(lambda r: r.method == "PUT") as info:
        sw.click()
    assert info.value.post_data_json == {
        "popup": True, "alarm": True, "telegram": False, "ntfy_topic": None,
    }  # fmt: skip
    expect(sw).to_have_attribute("aria-checked", "true")
    assert _get(live, m, "/me/alert-preferences")["alarm"] is True


def test_generate_ntfy_topic_persists_after_reload(page, live):
    m = _member(live)
    _open(page, m, "#/channels")
    with page.expect_response(re.compile(r"/ntfy-topic$")):
        page.get_by_role("button", name="Generate topic").click()
    topic = page.locator(".ntfy-topic")
    expect(topic).to_have_text(re.compile(r"^[A-Za-z0-9_-]{32}$"))
    saved = _get(live, m, "/me/alert-preferences")["ntfy_topic"]
    expect(topic).to_have_text(saved)
    page.reload()
    expect(page.locator(".ntfy-topic")).to_have_text(saved)

    with page.expect_response(lambda r: r.request.method == "PUT"):
        page.get_by_role("button", name="Remove topic").click()
    expect(page.locator(".ntfy-topic")).to_have_count(0)
    assert _get(live, m, "/me/alert-preferences")["ntfy_topic"] is None


def test_send_test_alert_shows_the_alert_screen(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _open(page, m, "#/channels")
    _wait_live(page)
    with page.expect_response(re.compile(r"/me/test-alert$")) as info:
        page.get_by_role("button", name="Send me a test alert").click()
    assert info.value.status == 202
    _deliver(live, info.value.json()["alert_id"])
    expect(page.get_by_role("alertdialog")).to_be_visible()
    expect(page.get_by_role("alertdialog").get_by_role("heading")).to_have_text("Test alert")


def test_test_alert_rate_limit_shows_retry_after(page, live):
    m = _member(live)
    for _ in range(3):
        assert httpx.post(f"{live.base_url}/me/test-alert", headers=m.headers).status_code == 202
    _open(page, m, "#/channels")
    page.get_by_role("button", name="Send me a test alert").click()
    expect(page.locator(".test-alert-msg")).to_have_text(re.compile(r"Try again in \d+ s"))


def test_telegram_not_configured_disables_the_link_button(page, live, monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", None)
    m = _member(live)
    _open(page, m, "#/channels")
    button = page.get_by_role("button", name="Link Telegram")
    button.click()
    expect(page.get_by_text("Telegram isn't set up on this server")).to_be_visible()
    expect(button).to_be_disabled()


def test_telegram_link_opens_a_validated_deep_link(page, live, monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123:abc")
    monkeypatch.setattr(get_settings(), "telegram_bot_username", "HomeBot")
    m = _member(live)
    _open(page, m, "#/channels")
    page.get_by_role("button", name="Link Telegram").click()
    link = page.get_by_role("link", name="Open Telegram")
    expect(link).to_have_attribute("href", re.compile(r"^https://t\.me/HomeBot\?start=\S+$"))
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")


def test_enable_browser_notifications_shows_the_result(new_page, live):
    page = _page(new_page)  # stubbed Notification: requestPermission -> "granted"
    m = _member(live)
    _open(page, m, "#/channels")
    page.get_by_role("button", name="Enable browser notifications").click()
    expect(page.locator(".notify-msg")).to_contain_text("allowed")
    assert page.evaluate("window.__permissionAsked") == 1


def test_notifications_not_enabled_copy_is_accurate(new_page, live):
    page = _page(new_page)
    page.add_init_script("Notification.permission = 'default'")
    m = _member(live)
    _open(page, m, "#/channels")
    expect(page.locator(".notify-msg")).to_have_text(
        "Not enabled yet: a background tab stays silent — you'll only see the alert when you "
        "switch back."
    )


def test_revoke_agent_token(page, live):
    m = _member(live)
    r = httpx.post(f"{live.base_url}/me/agent-tokens", json={"name": "Work laptop"},
                   headers=m.headers)  # fmt: skip
    assert r.status_code == 201, r.text
    _open(page, m, "#/channels")
    row = page.locator(".agent-row").filter(has_text="Work laptop")
    row.get_by_role("button", name="Revoke Work laptop").click()
    with page.expect_response(lambda r: r.request.method == "DELETE") as info:
        _dialog(page).get_by_role("button", name="Revoke").click()
    assert info.value.status == 204
    expect(page.locator(".agent-row")).to_have_count(0)
    assert _get(live, m, "/me/agent-tokens") == []


# --- household ---


def test_owner_changes_timezone_and_the_overview_follows(new_page, live):
    page = new_page(timezone_id="America/New_York")
    m = _member(live, timezone="UTC")
    _event(live, _device(live, m))
    received = _get(live, m, "/events")[0]["received_at"]
    _open(page, m, "#/household")
    with page.expect_response(lambda r: r.request.method == "PATCH") as info:
        page.get_by_label("Timezone").select_option("Asia/Tokyo")
    assert info.value.status == 200
    expect(page.locator(".tz-msg")).to_have_text("Timezone saved.")
    assert _get(live, m, "/households/me")["timezone"] == "Asia/Tokyo"
    page.get_by_role("link", name="Overview").click()
    _synced(page)
    tokyo = datetime.fromisoformat(received).astimezone(ZoneInfo("Asia/Tokyo"))
    cell = _row(page, "Doorbell").locator("td").first
    expect(cell).to_have_text(re.compile(re.escape(tokyo.strftime("%H:%M:%S")) + "$"))


def test_member_does_not_see_owner_controls(new_page, live):
    owner = _member(live, household="Maple House")
    member = _member(live, invite_token=_invite_token(live, owner))
    page = new_page()
    _open(page, member, "#/household")
    main = page.locator("main")
    expect(main.get_by_role("heading", name="Maple House")).to_be_visible()
    expect(main.locator(".member-row")).to_have_count(2)
    expect(main.locator(".member-row").filter(has_text=owner.email)).to_contain_text("Owner")
    for name in ("Create invite link", "Delete household", "Remove"):
        expect(main.get_by_role("button", name=name)).to_have_count(0)
    expect(main.get_by_role("combobox")).to_have_count(0)
    expect(main.get_by_role("button", name="Leave household")).to_be_visible()

    owner_page = new_page()
    _open(owner_page, owner, "#/household")
    expect(owner_page.get_by_role("button", name="Leave household")).to_have_count(0)
    expect(owner_page.get_by_role("button", name="Delete household")).to_be_visible()


def test_owner_creates_an_invite_link(new_page, live, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", live.base_url)
    owner = _member(live)
    page = new_page()
    _open(page, owner, "#/household")
    page.get_by_role("button", name="Create invite link").click()
    box = page.get_by_label("Invite link")
    expect(box).to_have_value(re.compile(rf"^{re.escape(live.base_url)}/app/#/join\?invite=\S+$"))
    expect(box).to_have_attribute("readonly", "")
    url = box.input_value()

    # the invite link opens the join view, and signing up there joins the household
    guest = new_page()
    guest.goto(url)
    expect(guest.get_by_role("heading", name="Join a household")).to_be_visible()
    assert "invite=" not in guest.url  # the one-time token leaves the address bar
    guest.get_by_label("Email").fill("guest@example.com")
    guest.get_by_label("Password").fill(PW)
    guest.get_by_role("button", name="Join").click()
    expect(guest.get_by_role("heading", name="Check your email")).to_be_visible()
    members = _get(live, owner, "/households/me")["members"]
    assert [x["role"] for x in members if x["email"] == "guest@example.com"] == ["member"]


def test_owner_removes_a_member(new_page, live):
    owner = _member(live)
    member = _member(live, invite_token=_invite_token(live, owner))
    page = new_page()
    _open(page, owner, "#/household")
    row = page.locator(".member-row").filter(has_text=member.email)
    row.get_by_role("button", name=f"Remove {member.email}").click()
    with page.expect_response(lambda r: r.request.method == "DELETE") as info:
        _dialog(page).get_by_role("button", name="Remove member").click()
    assert info.value.status == 204
    expect(page.locator(".member-row")).to_have_count(1)


def test_delete_household_requires_the_typed_name_and_logs_out(page, live):
    m = _member(live, household="Maple House")
    _open(page, m, "#/household")
    page.get_by_role("button", name="Delete household").click()
    dialog = _dialog(page)
    confirm = dialog.get_by_role("button", name="Delete household")
    expect(confirm).to_be_disabled()
    dialog.get_by_label("Type the household name to confirm").fill("Maple")
    expect(confirm).to_be_disabled()
    dialog.get_by_label("Type the household name to confirm").fill("Maple House")
    expect(confirm).to_be_enabled()
    confirm.click()
    expect(page).to_have_url(re.compile(r"#/login$"))
    expect(page.get_by_text("Your household was deleted.")).to_be_visible()
    r = httpx.get(f"{live.base_url}/households/me", headers=m.headers)
    assert r.status_code == 401


def test_member_leaves_and_is_logged_out(new_page, live):
    owner = _member(live)
    member = _member(live, invite_token=_invite_token(live, owner))
    page = new_page()
    _open(page, member, "#/household")
    page.get_by_role("button", name="Leave household").click()
    _dialog(page).get_by_role("button", name="Leave household").click()
    expect(page).to_have_url(re.compile(r"#/login$"))
    expect(page.get_by_text("You left the household.")).to_be_visible()
    assert len(_get(live, owner, "/households/me")["members"]) == 1


def test_server_strings_are_text(page, live):
    xss = "<img src=x onerror=alert(1)>"
    m = _member(live, household=xss)
    _device(live, m, name=xss)
    _open(page, m, "#/sensors")
    expect(page.locator(".device-row")).to_contain_text(xss)
    page.goto("/app/#/household")
    expect(page.locator("main").get_by_role("heading", name=xss)).to_be_visible()
    assert page.locator("main img").count() == 0


@pytest.mark.parametrize("hash_", ["#/sensors", "#/channels", "#/household"])
def test_phone_width_has_no_horizontal_overflow(new_page, live, hash_):
    page = new_page(viewport={"width": 360, "height": 740})
    owner = _member(live)
    _member(live, invite_token=_invite_token(live, owner))
    _device(live, owner, name="A sensor with a rather long name in the hallway")
    httpx.post(f"{live.base_url}/me/alert-preferences/ntfy-topic", headers=owner.headers)
    _open(page, owner, hash_)
    if hash_ == "#/sensors":
        page.get_by_label("Sensor name").fill("Kitchen")
        page.get_by_role("button", name="Create pairing code").click()
        expect(page.locator(".pair-code")).to_be_visible()
    if hash_ == "#/household":
        page.get_by_role("button", name="Create invite link").click()
        expect(page.get_by_label("Invite link")).to_be_visible()
    assert _no_overflow(page)
