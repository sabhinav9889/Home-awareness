"""Rooms, cameras, Away mode and alert photos in the dashboard: real browser, real API."""

import json
import re
import types
from datetime import UTC, datetime

import httpx
import pytest
from playwright.sync_api import expect

from tests.doubles import make_jpeg
from tests.web.test_alert_ui import (
    _deliver,
    _login,
    _page,
    _screen,
    _second_member,
)
from tests.web.test_overview import _member
from tests.web.test_settings_ui import _get, _no_overflow, _open

pytestmark = pytest.mark.web
CONSENT = "Everyone who may be on this camera knows about it"


# --- API-side helpers ---


def _call(live, member, method, path, **body):
    r = httpx.request(method, f"{live.base_url}{path}", headers=member.headers, json=body or None)
    assert r.status_code < 300, r.text
    return r.json() if r.content else None


def _room(live, member, name="Kitchen", kind="common"):
    return _call(live, member, "POST", "/rooms", name=name, kind=kind)


def _camera(live, member, name="Porch cam", room_id=None):
    body = {"name": name, "kind": "camera", "capabilities": "video", "consent_confirmed": True}
    if room_id is not None:
        body["room_id"] = room_id
    code = _call(live, member, "POST", "/devices/pairing-codes", **body)["code"]
    r = httpx.post(f"{live.base_url}/device/pair", json={"code": code})
    assert r.status_code == 201, r.text
    paired = r.json()
    return types.SimpleNamespace(id=paired["device_id"], key={"X-Device-Key": paired["device_key"]})


def _camera_event(live, camera, *, image=True, type_="glass_breaking"):
    """A camera event that alerts; returns (event_id, alert ids), not yet delivered."""
    before = len(live.jobs.enqueued)
    event = {"type": type_, "confidence": 0.9, "occurred_at": datetime.now(UTC).isoformat()}
    files = {"event": (None, json.dumps(event))}
    if image:
        files["snapshot"] = ("s.jpg", make_jpeg(), "image/jpeg")
    r = httpx.post(f"{live.base_url}/device/events", files=files, headers=camera.key)
    assert r.status_code == 202, r.text
    assert r.json()["alerts"] >= 1, r.json()
    return r.json()["event_id"], live.jobs.enqueued[before:]


REVOKE_SPY = """
window.__revoked = [];
const origRevoke = URL.revokeObjectURL.bind(URL);
URL.revokeObjectURL = (u) => { window.__revoked.push(u); origRevoke(u); };
"""


# --- rooms ---


def _card(page, name):
    return page.locator(".room-card").filter(has=page.locator(".room-name", has_text=name))


def test_create_room_then_assign_a_sensor_to_it(page, live):
    m = _member(live)
    from tests.web.test_overview import _device

    _device(live, m, name="Hall")
    _open(page, m, "#/rooms")
    page.get_by_label("Room name").fill("Kitchen")
    page.get_by_label("Room type").select_option("common")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/rooms")):
        page.get_by_role("button", name="Add room").click()
    card = _card(page, "Kitchen")
    expect(card).to_be_visible()
    [room] = _get(live, m, "/rooms")

    page.goto("/app/#/sensors")
    select = page.get_by_label("Room for Hall")
    expect(select).to_be_visible()
    with page.expect_response(lambda r: r.request.method == "PATCH" and "/devices/" in r.url):
        select.select_option(label="Kitchen")
    [d] = _get(live, m, "/devices")
    assert d["room_id"] == room["id"]
    page.goto("/app/#/rooms")
    expect(_card(page, "Kitchen")).to_contain_text("Hall")


def test_rename_and_delete_a_room(page, live):
    m = _member(live)
    _room(live, m, "Den")
    _open(page, m, "#/rooms")
    page.get_by_role("button", name="Rename Den").click()
    page.get_by_label("New name for Den").fill("Study")
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        page.get_by_role("button", name="Save name").click()
    expect(_card(page, "Study")).to_be_visible()
    assert [r["name"] for r in _get(live, m, "/rooms")] == ["Study"]

    page.get_by_role("button", name="Delete Study").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete room").click()
    expect(page.locator(".room-card")).to_have_count(0)
    assert _get(live, m, "/rooms") == []


def test_room_names_are_text_not_html(page, live):
    m = _member(live)
    _room(live, m, "<img src=x onerror=alert(1)>")
    _open(page, m, "#/rooms")
    expect(page.locator(".room-card")).to_contain_text("<img src=x onerror=alert(1)>")
    assert page.locator(".room-card img").count() == 0


def test_member_sees_no_room_controls(new_page, live):
    owner = _member(live)
    _room(live, owner, "Kitchen")
    member = _second_member(live, owner)
    page = new_page()
    _open(page, member, "#/rooms")
    expect(_card(page, "Kitchen")).to_be_visible()
    expect(page.get_by_role("button", name="Add room")).to_have_count(0)
    expect(page.get_by_role("button", name="Rename Kitchen")).to_have_count(0)
    expect(page.get_by_role("button", name="Delete Kitchen")).to_have_count(0)


# --- the camera form ---


def test_adding_a_camera_requires_room_and_consent(page, live):
    m = _member(live)
    room = _room(live, m, "Porch", "entrance")
    _open(page, m, "#/sensors")
    seen = []
    page.on("request", lambda r: seen.append(r.url) if "pairing-codes" in r.url else None)
    page.get_by_label("Camera name").fill("Porch cam")
    page.get_by_role("button", name="Create camera pairing code").click()
    expect(page.locator("#cam-form-msg [role='alert']")).to_contain_text("room")
    page.get_by_label("Room", exact=True).select_option(str(room["id"]))
    page.get_by_role("button", name="Create camera pairing code").click()
    expect(page.locator("#cam-form-msg [role='alert']")).to_contain_text("knows")
    assert seen == []

    page.get_by_label(CONSENT).check()
    with page.expect_response(re.compile(r"/devices/pairing-codes$")) as info:
        page.get_by_role("button", name="Create camera pairing code").click()
    assert info.value.status == 201
    code = page.locator("#cam-form-result .pair-code").inner_text()
    r = httpx.post(f"{live.base_url}/device/pair", json={"code": code})
    assert r.status_code == 201
    [d] = _get(live, m, "/devices")
    assert (d["kind"], d["capabilities"], d["room_id"]) == ("camera", "video", room["id"])
    assert d["consent_confirmed_at"] is not None


def test_private_room_gets_a_stronger_notice(page, live):
    m = _member(live)
    _room(live, m, "Porch", "entrance")
    bed = _room(live, m, "Bedroom", "bedroom")
    _open(page, m, "#/sensors")
    notice = page.locator(".room-notice")
    select = page.get_by_label("Room", exact=True)
    select.select_option(label="Porch")
    expect(notice).not_to_contain_text("off by default")
    select.select_option(str(bed["id"]))
    expect(notice).to_contain_text("private")
    expect(notice).to_contain_text("off by default")


def test_camera_card_shows_room_consent_and_the_owner_snapshot_switch(page, live):
    m = _member(live)
    room = _room(live, m, "Porch", "entrance")
    cam = _camera(live, m, room_id=room["id"])
    _open(page, m, "#/sensors")
    row = page.locator(".device-row").filter(has_text="Porch cam")
    expect(row).to_contain_text("Porch")
    expect(row).to_contain_text("Everyone on camera knows")
    sw = row.get_by_role("switch", name="Send snapshots from Porch cam")
    expect(sw).to_have_attribute("aria-checked", "true")
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        sw.click()
    expect(sw).to_have_attribute("aria-checked", "false")
    [d] = _get(live, m, "/devices")
    assert d["id"] == cam.id and d["send_snapshots"] is False


def test_private_room_camera_starts_with_snapshots_off(page, live):
    m = _member(live)
    bed = _room(live, m, "Bedroom", "bedroom")
    _camera(live, m, "Bed cam", room_id=bed["id"])
    _open(page, m, "#/sensors")
    row = page.locator(".device-row").filter(has_text="Bed cam")
    expect(row.get_by_role("switch")).to_have_attribute("aria-checked", "false")


def test_member_sees_snapshot_state_but_no_owner_controls(new_page, live):
    owner = _member(live)
    _camera(live, owner, "Porch cam")
    member = _second_member(live, owner)
    page = new_page()
    _open(page, member, "#/sensors")
    row = page.locator(".device-row").filter(has_text="Porch cam")
    expect(row).to_contain_text("Snapshots: on")
    expect(row.get_by_role("switch")).to_have_count(0)
    page.goto("/app/#/household")
    expect(page.get_by_role("heading", level=1, name="Household")).to_be_visible()
    expect(page.get_by_label("Night starts")).to_have_count(0)
    expect(page.get_by_label("Keep alert photos for")).to_have_count(0)


# --- Away mode ---


def test_away_toggle_updates_a_second_tab_and_says_who(new_page, live):
    m = _member(live)
    a, b = new_page(), new_page()
    _login(a, m)
    _login(b, m)
    away_a = a.get_by_role("button", name="Away", exact=True)
    away_b = b.get_by_role("button", name="Away", exact=True)
    expect(away_a).to_have_attribute("aria-pressed", "false")
    expect(a.locator(".guard-state")).to_have_text("Home")
    away_a.click()
    expect(away_a).to_have_attribute("aria-pressed", "true")
    expect(away_b).to_have_attribute("aria-pressed", "true")
    expect(b.locator(".guard-state")).to_contain_text("Away — guarding")
    expect(b.locator(".guard-notice")).to_have_text(f"{m.email} turned Away on")
    assert _get(live, m, "/households/me")["away"] is True
    away_b.click()
    expect(away_a).to_have_attribute("aria-pressed", "false")
    expect(a.locator(".guard-notice")).to_have_text(f"{m.email} turned Away off")


def test_away_state_shows_night_hours(page, live):
    m = _member(live)
    _call(live, m, "PATCH", "/households/me", night_start="22:00:00", night_end="06:00:00")
    _login(page, m)
    expect(page.locator(".guard-state")).to_contain_text("22:00")
    expect(page.locator(".guard-state")).to_contain_text("06:00")


def test_a_member_can_turn_away_on(new_page, live):
    owner = _member(live)
    member = _second_member(live, owner)
    page = new_page()
    _login(page, member)
    page.get_by_role("button", name="Away", exact=True).click()
    expect(page.locator(".guard-state")).to_contain_text("Away — guarding")
    assert _get(live, owner, "/households/me")["away"] is True


# --- household: night hours and retention ---


def test_owner_sets_and_clears_night_hours(page, live):
    m = _member(live)
    _open(page, m, "#/household")
    page.get_by_label("Night starts").fill("22:30")
    page.get_by_label("Night ends").fill("06:15")
    with page.expect_response(lambda r: r.request.method == "PATCH") as info:
        page.get_by_role("button", name="Save night hours").click()
    assert info.value.status == 200
    h = _get(live, m, "/households/me")
    assert (h["night_start"], h["night_end"]) == ("22:30:00", "06:15:00")

    page.get_by_label("Night starts").fill("")
    page.get_by_label("Night ends").fill("")
    page.get_by_role("button", name="Save night hours").click()
    expect(page.locator(".night-msg")).to_contain_text("off")
    h = _get(live, m, "/households/me")
    assert (h["night_start"], h["night_end"]) == (None, None)


def test_one_night_time_alone_is_refused_in_the_page(page, live):
    m = _member(live)
    _open(page, m, "#/household")
    page.get_by_label("Night starts").fill("22:00")
    page.get_by_role("button", name="Save night hours").click()
    expect(page.locator(".night-msg")).to_contain_text("both")
    assert _get(live, m, "/households/me")["night_start"] is None


def test_owner_changes_photo_retention(page, live):
    m = _member(live)
    _open(page, m, "#/household")
    select = page.get_by_label("Keep alert photos for")
    expect(select).to_have_value("7")
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        select.select_option("30")
    assert _get(live, m, "/households/me")["snapshot_retention_days"] == 30


# --- photos ---


def test_alert_with_a_snapshot_shows_a_blob_image_and_revokes_it(new_page, live):
    m = _member(live)
    cam = _camera(live, m)
    page = _page(new_page)
    page.add_init_script(REVOKE_SPY)
    _login(page, m)
    event_id, alert_ids = _camera_event(live, cam)
    _deliver(live, *alert_ids)
    img = _screen(page).locator("img.alert-photo")
    expect(img).to_have_count(1)
    src = img.get_attribute("src")
    assert src.startswith("blob:") and m.token not in src
    assert page.evaluate("window.__revoked.length") == 0
    _screen(page).get_by_role("button", name="Dismiss").click()
    expect(_screen(page)).to_have_count(0)
    assert page.evaluate("window.__revoked") == [src]


def test_alert_without_a_snapshot_says_no_photo(new_page, live):
    m = _member(live)
    cam = _camera(live, m)
    page = _page(new_page)
    _login(page, m)
    _, alert_ids = _camera_event(live, cam, image=False)
    _deliver(live, *alert_ids)
    expect(_screen(page)).to_contain_text("No photo")
    assert _screen(page).locator("img").count() == 0


def test_live_event_row_offers_its_photo(new_page, live):
    m = _member(live)
    cam = _camera(live, m)
    page = _page(new_page)
    _login(page, m)
    _camera_event(live, cam)
    row = page.get_by_role("table", name="Live events").locator("tbody tr").first
    with page.expect_response(re.compile(r"/snapshots/\d+$")) as info:
        row.get_by_role("button", name="Photo").click()
    assert info.value.status == 200
    expect(page.locator(".event-photo img")).to_have_attribute("src", re.compile(r"^blob:"))


def test_the_sounds_page_shows_the_camera_groups(page, live):
    m = _member(live)
    page.goto("/app/#/login")
    page.evaluate("t => sessionStorage.setItem('ha.token', t)", m.token)
    page.goto("/app/#/sounds")
    expect(page.locator("[data-sound='dog_barking']")).to_have_count(1)
    for group in ("Health", "Security", "Incidents"):
        expect(page.locator(".snd-group-title", has_text=group)).to_have_count(1)
    expect(page.locator(".snd-group")).to_have_count(page.locator(".snd-group-title").count())


def test_footer_mentions_camera_snapshots(page, live):
    m = _member(live)
    _open(page, m, "#/rooms")
    expect(page.locator(".disclaimer")).to_contain_text(
        "Cameras send a single snapshot only when they raise an alert."
    )


# --- phone width ---


@pytest.mark.parametrize("hash_", ["#/rooms", "#/sensors", "#/household", "#/overview"])
def test_no_horizontal_overflow_at_360(new_page, live, hash_):
    page = new_page(viewport={"width": 360, "height": 780})
    m = _member(live, household="A Very Long Household Name For The Audit " * 2)
    bed = _room(live, m, "A bedroom with a very long name indeed, really long", "bedroom")
    _camera(live, m, "Camera with a very long name that keeps going on", room_id=bed["id"])
    _open_any(page, m, hash_)
    if hash_ == "#/sensors":
        page.get_by_label("Room", exact=True).select_option(str(bed["id"]))
        expect(page.locator(".room-notice")).to_contain_text("off by default")
    assert _no_overflow(page)
    from tests.web.test_layout_audit import OFFENDERS

    assert page.evaluate(OFFENDERS) == []


def _open_any(page, member, hash_):
    if hash_ == "#/overview":
        _login(page, member)
    else:
        _open(page, member, hash_)


# --- final review fixes ---


def test_history_rows_loaded_from_the_server_offer_their_photo(new_page, live):
    m = _member(live)
    cam = _camera(live, m)
    _camera_event(live, cam)
    _camera_event(live, cam, image=False, type_="fall")
    page = _page(new_page)
    _login(page, m)
    rows = page.get_by_role("table", name="Live events").locator("tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.get_by_role("button", name="Photo")).to_have_count(1)  # only the one with a photo


def test_a_failed_photo_fetch_is_retried_on_reopen(new_page, live):
    m = _member(live)
    cam = _camera(live, m)
    page = _page(new_page)
    _login(page, m)
    _camera_event(live, cam)
    calls = []

    def flaky(route):
        calls.append(1)
        if len(calls) == 1:
            route.fulfill(status=500, content_type="application/json", body='{"detail":"x"}')
        else:
            route.continue_()

    page.route(re.compile(r"/snapshots/\d+$"), flaky)
    btn = page.get_by_role("table", name="Live events").get_by_role("button", name="Photo")
    btn.click()
    expect(page.locator(".event-photo")).to_contain_text("Couldn't load the photo")
    btn.click()  # close
    expect(page.locator(".event-photo")).to_have_count(0)
    btn.click()  # reopen: fetched again
    expect(page.locator(".event-photo img")).to_have_attribute("src", re.compile(r"^blob:"))
    assert len(calls) == 2


def test_away_notice_shows_on_the_sensors_page_too(new_page, live):
    m = _member(live)
    other = _second_member(live, m)
    page = new_page()
    _login(page, m, "#/sensors")
    expect(page.locator(".guard-notice")).to_have_text("")
    _call(live, other, "PATCH", "/households/me", away=True)
    expect(page.locator(".guard-notice")).to_have_text(f"{other.email} turned Away on")
    _call(live, other, "PATCH", "/households/me", away=False)
    expect(page.locator(".guard-notice")).to_have_text(f"{other.email} turned Away off")


def test_sensor_list_refresh_waits_while_focus_is_inside_it(page, live):
    from tests.web.test_overview import _device, _go_offline, _heartbeat

    m = _member(live)
    hall = _device(live, m, "Hall")
    _heartbeat(live, hall)
    _room(live, m, "Kitchen")
    _open(page, m, "#/sensors")
    row = page.locator(".device-row").filter(has_text="Hall")
    expect(row).to_contain_text("Listening")
    select = page.get_by_label("Room for Hall")
    select.focus()
    page.evaluate("document.activeElement.dataset.mark = 'same'")
    with page.expect_response(lambda r: r.url.endswith("/rooms") and r.request.method == "GET"):
        _go_offline(live, hall.id)  # publishes device_offline: the view refreshes
    page.wait_for_timeout(300)
    # The list was not rebuilt under the focused select, so the old state still shows.
    assert page.evaluate("document.activeElement.dataset.mark") == "same"
    expect(row).to_contain_text("Listening")
    page.get_by_label("Camera name").focus()  # focus leaves the list: the refresh lands
    expect(page.locator(".device-row").filter(has_text="Hall")).to_contain_text("Offline")


def test_camera_kind_is_labelled_on_the_overview(page, live):
    m = _member(live)
    _camera(live, m)
    from tests.web.test_overview import _open as open_overview

    open_overview(page, m)
    expect(page.locator(".sensor-card")).to_contain_text("Camera")
