"""The room in alerts and live rows, and the room picker, name suggestion and badges on the
Sensors page: real browser, real API."""

import json
import re
import types

import httpx
import pytest
from playwright.sync_api import expect

from tests.web.test_alert_ui import (
    _deliver,
    _event,
    _login,
    _page,
    _screen,
)
from tests.web.test_camera_ui import _room
from tests.web.test_overview import _device as _plain_device
from tests.web.test_overview import _event as _overview_event
from tests.web.test_overview import _member, _rows, _synced
from tests.web.test_settings_ui import _get, _no_overflow, _open

pytestmark = pytest.mark.web


def _sensor(live, member, name, *, room_id=None, capabilities="audio", kind="pi"):
    body = {"name": name, "kind": kind, "capabilities": capabilities}
    if "video" in capabilities:
        body["consent_confirmed"] = True
    if room_id is not None:
        body["room_id"] = room_id
    r = httpx.post(f"{live.base_url}/devices/pairing-codes", json=body, headers=member.headers)
    assert r.status_code == 201, r.text
    r = httpx.post(f"{live.base_url}/device/pair", json={"code": r.json()["code"]})
    assert r.status_code == 201, r.text
    paired = r.json()
    return types.SimpleNamespace(id=paired["device_id"], key={"X-Device-Key": paired["device_key"]})


# --- the alert popup ---


def test_popup_shows_the_room_next_to_the_sensor(new_page, live):
    page = _page(new_page)
    m = _member(live)
    room = _room(live, m, "Living room")
    mic = _sensor(live, m, "Living room mic", room_id=room["id"])
    _login(page, m)
    _, alert_ids = _event(live, mic)
    _deliver(live, *alert_ids)
    screen = _screen(page)
    expect(screen.locator(".alert-device")).to_have_text("Living room · via Living room mic")
    expect(screen.locator(".alert-room")).to_have_text("Living room")
    # announced too, not only drawn
    expect(screen).to_have_accessible_description(re.compile(r"Living room · via Living room mic"))


def test_popup_without_a_room_shows_only_the_sensor(new_page, live):
    page = _page(new_page)
    m = _member(live)
    mic = _sensor(live, m, "Hall mic")
    _login(page, m)
    _, alert_ids = _event(live, mic)
    _deliver(live, *alert_ids)
    expect(_screen(page).locator(".alert-device")).to_have_text("Hall mic")
    expect(_screen(page).locator(".alert-room")).to_have_count(0)
    expect(_screen(page)).to_have_attribute("aria-describedby", "alert-where alert-desc")


def test_catch_up_alert_shows_the_room(new_page, live):
    page = _page(new_page)
    m = _member(live)
    room = _room(live, m, "Kitchen")
    mic = _sensor(live, m, "Kitchen mic", room_id=room["id"])
    _event(live, mic)  # raised while no dashboard is open: never delivered live
    _login(page, m)  # the connect-time catch-up replays it from GET /alerts
    expect(_screen(page).locator(".alert-device")).to_have_text("Kitchen · via Kitchen mic")


def test_test_alert_has_no_room_line(new_page, live):
    from tests.web.test_alert_ui import _test_alert

    page = _page(new_page)
    m = _member(live)
    _login(page, m)
    _test_alert(live, m)
    expect(_screen(page)).to_be_visible()
    expect(_screen(page).locator(".alert-device")).to_have_count(0)
    # no dangling id for the "where" line a test alert doesn't have
    expect(_screen(page)).to_have_attribute("aria-describedby", "alert-desc")


def test_popup_room_is_text_not_html_and_fits_a_phone(new_page, live):
    page = _page(new_page, viewport={"width": 360, "height": 780})
    m = _member(live)
    nasty = "<img src=x onerror=alert(1)>" + "R" * 60
    room = _room(live, m, nasty[:60])
    mic = _sensor(live, m, "M" * 70, room_id=room["id"])
    _login(page, m)
    _, alert_ids = _event(live, mic)
    _deliver(live, *alert_ids)
    expect(_screen(page).locator(".alert-device")).to_contain_text("<img src=x")
    assert _screen(page).locator("img").count() == 0
    assert _no_overflow(page)


# --- overview ---


def test_overview_live_row_shows_the_room(page, live):
    m = _member(live)
    room = _room(live, m, "Kitchen")
    mic = _sensor(live, m, "Kitchen mic", room_id=room["id"])
    bare = _plain_device(live, m, name="Loose mic")
    page.goto("/app/#/login")
    page.evaluate("t => sessionStorage.setItem('ha.token', t)", m.token)
    page.goto("/app/#/overview")
    _synced(page)
    _overview_event(live, mic)
    row = _rows(page).filter(has_text="Kitchen mic")
    expect(row).to_have_count(1)
    expect(row.locator(".cell-room")).to_have_text("Kitchen")
    expect(row).to_contain_text("Kitchen mic · Kitchen")
    _overview_event(live, bare)
    loose = _rows(page).filter(has_text="Loose mic")
    expect(loose).to_have_count(1)
    expect(loose.locator(".cell-room")).to_have_count(0)


# --- the Sensors page: room picker, suggested names, badges, hint ---


def _pair_request(page, click):
    with page.expect_request(re.compile(r"/devices/pairing-codes$")) as info:
        click()
    return json.loads(info.value.post_data)


def test_add_a_sensor_with_a_room_sends_room_id_and_suggests_a_name(page, live):
    m = _member(live)
    kitchen = _room(live, m, "Kitchen")
    _room(live, m, "Hall")
    _open(page, m, "#/sensors")
    name = page.get_by_label("Sensor name")
    picker = page.get_by_label("Room (optional)")
    expect(name).to_have_value("")
    picker.select_option(label="Kitchen")
    expect(name).to_have_value("Kitchen mic")
    picker.select_option(label="Hall")
    expect(name).to_have_value("Hall mic")  # follows the room while untouched
    picker.select_option(label="Kitchen")
    body = _pair_request(
        page, lambda: page.get_by_role("button", name="Create pairing code").click()
    )
    assert body["room_id"] == kitchen["id"] and body["name"] == "Kitchen mic"


def test_name_is_re_suggested_after_a_submit_and_a_vanished_room_clears_it(page, live):
    m = _member(live)
    kitchen = _room(live, m, "Kitchen")
    _open(page, m, "#/sensors")
    name = page.get_by_label("Sensor name")
    page.get_by_label("Room (optional)").select_option(label="Kitchen")
    expect(name).to_have_value("Kitchen mic")
    page.get_by_role("button", name="Create pairing code").click()
    expect(page.locator(".pair-result .pair-code")).to_be_visible()
    expect(name).to_have_value("Kitchen mic")  # room still selected: the next name is offered
    httpx.delete(f"{live.base_url}/rooms/{kitchen['id']}", headers=m.headers)
    expect(page.get_by_label("Room (optional)")).to_have_value("", timeout=15000)
    expect(name).to_have_value("")  # the stale suggestion went with the room


def test_a_typed_name_is_never_overwritten(page, live):
    m = _member(live)
    _room(live, m, "Kitchen")
    _room(live, m, "Hall")
    _open(page, m, "#/sensors")
    name = page.get_by_label("Sensor name")
    name.fill("My old laptop")
    page.get_by_label("Room (optional)").select_option(label="Kitchen")
    expect(name).to_have_value("My old laptop")
    page.get_by_label("Room (optional)").select_option(label="Hall")
    expect(name).to_have_value("My old laptop")


def test_a_sensor_without_a_room_sends_no_room_id(page, live):
    m = _member(live)
    _room(live, m, "Kitchen")
    _open(page, m, "#/sensors")
    page.get_by_label("Sensor name").fill("Loose")
    body = _pair_request(
        page, lambda: page.get_by_role("button", name="Create pairing code").click()
    )
    assert "room_id" not in body or body["room_id"] is None


def test_no_rooms_links_to_the_rooms_page(page, live):
    m = _member(live)
    _open(page, m, "#/sensors")
    link = page.locator(".pair-form").get_by_role("link", name="Rooms")
    expect(link).to_have_attribute("href", "#/rooms")


def test_camera_form_suggests_a_camera_name_without_overwriting(page, live):
    m = _member(live)
    _room(live, m, "Porch", "entrance")
    _room(live, m, "Garage", "other")
    _open(page, m, "#/sensors")
    name = page.get_by_label("Camera name")
    picker = page.get_by_label("Room", exact=True)
    picker.select_option(label="Porch")
    expect(name).to_have_value("Porch camera")
    picker.select_option(label="Garage")
    expect(name).to_have_value("Garage camera")
    name.fill("Front door")
    picker.select_option(label="Porch")
    expect(name).to_have_value("Front door")


def test_capability_badges(page, live):
    m = _member(live)
    _sensor(live, m, "Alpha", capabilities="audio")
    _sensor(live, m, "Bravo", capabilities="video", kind="camera")
    _sensor(live, m, "Charlie", capabilities="audio,video", kind="camera")
    _open(page, m, "#/sensors")

    def badge(name):
        return page.locator(".device-row").filter(has_text=name).locator(".cap-badge")

    expect(badge("Alpha")).to_have_text("Microphone")
    expect(badge("Bravo")).to_have_count(0)  # kind label already says "Camera"
    expect(page.locator(".device-row").filter(has_text="Bravo")).to_contain_text("Camera")
    expect(badge("Charlie")).to_have_text("Microphone + Camera")


def test_no_room_hint_goes_away_after_assignment(page, live):
    m = _member(live)
    _room(live, m, "Kitchen")
    _sensor(live, m, "Loose mic")
    _open(page, m, "#/sensors")
    row = page.locator(".device-row").filter(has_text="Loose mic")
    hint = row.locator(".room-hint")
    expect(hint).to_have_text("No room — assign one so it can join incidents")
    hint.get_by_role("button").click()  # moves to the card's own room select
    expect(row.get_by_label("Room for Loose mic")).to_be_focused()
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        row.get_by_label("Room for Loose mic").select_option(label="Kitchen")
    expect(row.locator(".room-hint")).to_have_count(0)
    assert _get(live, m, "/devices")[0]["room_name"] == "Kitchen"


def test_rooms_page_shows_the_badge(page, live):
    m = _member(live)
    room = _room(live, m, "Kitchen")
    _sensor(live, m, "Kitchen mic", room_id=room["id"])
    _open(page, m, "#/rooms")
    expect(page.locator(".room-device .cap-badge")).to_have_text("Microphone")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_sensors_view_has_no_overflow_at_360_with_long_names(new_page, live, scheme):
    page = new_page(viewport={"width": 360, "height": 780}, color_scheme=scheme)
    m = _member(live)
    long_room = _room(live, m, "R" * 60)
    spaced = _room(live, m, "An extremely long room name with many words in it to wrap")
    _sensor(live, m, "D" * 80, room_id=long_room["id"], capabilities="audio,video", kind="camera")
    _sensor(live, m, "A very long sensor name " * 3, room_id=spaced["id"])
    _sensor(live, m, "N" * 80)  # no room: shows the hint
    _open(page, m, "#/sensors")
    page.get_by_label("Room (optional)").select_option(label=spaced["name"])
    expect(page.locator(".device-row")).to_have_count(3)
    assert _no_overflow(page)
    offenders = page.evaluate(
        """() => [...document.querySelectorAll('main *')].filter(
          (e) => e.getBoundingClientRect().right > window.innerWidth + 1).length"""
    )
    assert offenders == 0
