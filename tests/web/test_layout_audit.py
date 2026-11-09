"""Phone-width audit: no view scrolls the page sideways, in either theme or width."""

import httpx
import pytest
from playwright.sync_api import expect

from tests.web.test_camera_ui import _room
from tests.web.test_overview import _device, _event, _heartbeat, _member

pytestmark = pytest.mark.web

PRIVATE = ["#/overview", "#/sounds", "#/rooms", "#/sensors", "#/channels", "#/household"]
PUBLIC = ["#/login", "#/signup"]
SIZES = [(360, 780), (1280, 800)]

# Elements whose right edge passes the viewport and that no scrolling ancestor contains.
OFFENDERS = """() => {
  const w = window.innerWidth;
  const scrolls = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll') return true;
    }
    return false;
  };
  const bad = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('svg') && el.tagName !== 'svg') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right > w + 1 && !scrolls(el)) bad.push(el.tagName + '.' + el.className + ' ' + r.right);
  }
  return bad;
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("hash_", PRIVATE + PUBLIC)
def test_no_horizontal_overflow(new_page, live, hash_, size, scheme):
    page = new_page(viewport={"width": size[0], "height": size[1]}, color_scheme=scheme)
    if hash_ in PRIVATE:
        m = _member(live, household="A Very Long Household Name For The Audit " * 2)
        d = _device(live, m, name="Living room sensor with a very long name indeed")
        room = _room(live, m, "A living room whose name is long enough to need wrapping")
        r = httpx.patch(
            f"{live.base_url}/devices/{d.id}", json={"room_id": room["id"]}, headers=m.headers
        )
        assert r.status_code == 200, r.text
        _heartbeat(live, d)
        _event(live, d, "doorbell")
        page.goto("/app/#/login")
        page.evaluate("t => sessionStorage.setItem('ha.token', t)", m.token)
    page.goto(f"/app/{hash_}")
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    ready = {
        "#/overview": "section.events[aria-busy='false']",
        "#/sounds": ".snd-row",
        "#/rooms": "main [data-ready='true']",
        "#/sensors": "main [data-ready='true']",
        "#/channels": "main [data-ready='true']",
        "#/household": "main [data-ready='true']",
    }.get(hash_)
    if ready:
        expect(page.locator(ready).first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate(OFFENDERS) == []
    expected = "dark" if scheme == "dark" else "light"
    assert page.evaluate(f"matchMedia('(prefers-color-scheme: {expected})').matches")


def test_dark_scheme_changes_the_palette_and_keeps_text_readable(new_page, live):
    def colours(scheme):
        page = new_page(color_scheme=scheme)
        page.goto("/app/#/login")
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        return page.evaluate(
            """() => {
              const cs = (e) => getComputedStyle(e);
              return {
                bg: cs(document.body).backgroundColor, ink: cs(document.body).color,
                card: cs(document.querySelector('.card')).backgroundColor,
              };
            }"""
        )

    light, dark = colours("light"), colours("dark")
    assert light["bg"] != dark["bg"] and light["card"] != dark["card"]

    def lum(rgb):
        v = [int(x) for x in rgb[rgb.index("(") + 1 : -1].split(",")[:3]]
        v = [c / 255 for c in v]
        f = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in v]
        return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]

    for theme in (light, dark):
        hi, lo = sorted((lum(theme["ink"]), lum(theme["bg"])), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= 4.5
