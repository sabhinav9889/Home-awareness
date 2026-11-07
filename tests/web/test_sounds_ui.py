"""Sounds & rules view: real browser, real API."""

import httpx
import pytest
from playwright.sync_api import expect

from tests.web.test_overview import _member

pytestmark = pytest.mark.web


def _open(page, member):
    page.goto("/app/#/login")
    page.evaluate("t => sessionStorage.setItem('ha.token', t)", member.token)
    page.goto("/app/#/sounds")
    expect(page.locator("[data-sound='dog_barking']")).to_have_count(1)


def _row(page, key):
    return page.locator(f"[data-sound='{key}']")


def _settings(live, member, key):
    r = httpx.get(f"{live.base_url}/me/sound-settings", headers=member.headers)
    assert r.status_code == 200, r.text
    return next(s for s in r.json() if s["sound_key"] == key)


def _saved(page):
    return page.expect_response(lambda r: r.request.method == "PUT" and "sound-settings" in r.url)


def test_toggle_dog_barking_persists_across_reload(page, live):
    m = _member(live)
    _open(page, m)
    sw = _row(page, "dog_barking").get_by_role("switch")
    expect(sw).to_have_attribute("aria-checked", "false")
    with _saved(page) as info:
        sw.click()
    assert info.value.status == 200
    assert _settings(live, m, "dog_barking")["enabled"] is True
    page.reload()
    expect(_row(page, "dog_barking").get_by_role("switch")).to_have_attribute(
        "aria-checked", "true"
    )


def test_invalid_repeat_shows_the_422_message_and_saves_nothing(page, live):
    m = _member(live)
    _open(page, m)
    row = _row(page, "dog_barking")
    before = _settings(live, m, "dog_barking")["repeat_count"]
    with _saved(page) as info:
        row.get_by_label("Alert after (times)").fill("0")
    assert info.value.status == 422
    expect(row.locator(".row-error")).to_contain_text("greater than or equal to 1")
    assert _settings(live, m, "dog_barking")["repeat_count"] == before
    # fixing the value clears the message
    with _saved(page):
        row.get_by_label("Alert after (times)").fill("4")
    expect(row.locator(".row-error")).to_have_count(0)
    assert _settings(live, m, "dog_barking")["repeat_count"] == 4


def test_badges_render_for_scream(page, live):
    m = _member(live)
    _open(page, m)
    row = _row(page, "scream")
    expect(row.locator(".badge-urgent")).to_have_text("Urgent")
    expect(row.locator(".badge-rel")).to_have_text("good")
    expect(_row(page, "moaning").locator(".badge-rel")).to_have_text("experimental")
    expect(_row(page, "dog_barking").locator(".badge-urgent")).to_have_count(0)


def test_min_confidence_persists_and_reset_clears_it(page, live):
    m = _member(live)
    _open(page, m)
    row = _row(page, "scream")
    box = row.get_by_label("Min confidence")
    default = float(box.input_value())
    with _saved(page):
        box.fill("0.65")
    assert _settings(live, m, "scream")["min_confidence"] == 0.65
    page.reload()
    expect(_row(page, "scream").get_by_label("Min confidence")).to_have_value("0.65")
    with _saved(page):
        _row(page, "scream").get_by_role("button", name="Reset").click()
    assert _settings(live, m, "scream")["min_confidence"] == default
    expect(_row(page, "scream").get_by_label("Min confidence")).to_have_value(str(default))


def test_unrelated_save_keeps_min_confidence_override(page, live):
    m = _member(live)
    _open(page, m)
    row = _row(page, "scream")
    with _saved(page):
        row.get_by_label("Min confidence").fill("0.65")
    with _saved(page):
        row.get_by_label("Alert after (times)").fill("3")
    s = _settings(live, m, "scream")
    assert (s["repeat_count"], s["min_confidence"]) == (3, 0.65)


def test_phone_width_has_no_horizontal_overflow(new_page, live):
    page = new_page(viewport={"width": 360, "height": 740})
    m = _member(live)
    _open(page, m)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert "Sensors only listen for sounds someone in your household turned on." in (
        page.locator("main").inner_text()
    )
