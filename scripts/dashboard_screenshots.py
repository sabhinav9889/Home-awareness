"""Regenerate docs/screenshots/*.png: the dashboard on a seeded demo household.

Not a test. Starts the app the way the web tests do (real app under uvicorn, the `home_test`
database, fakeredis, in-memory outbox), seeds made-up sensors, events and alerts (no real
data), drives Chrome through every view at phone and laptop size, and truncates the tables
again at the end. Needs Postgres and Google Chrome:

    uv run python scripts/dashboard_screenshots.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests.conftest as db  # noqa: E402  (after sys.path)
from tests.web.conftest import _run, _truncate_all, run_live_server  # noqa: E402
from tests.web.test_alert_ui import STUBS, _deliver  # noqa: E402
from tests.web.test_alert_ui import _event as _alerting_event  # noqa: E402
from tests.web.test_overview import (  # noqa: E402
    _device,
    _event,
    _go_offline,
    _heartbeat,
    _member,
)

OUT = ROOT / "docs" / "screenshots"
SIZES = {"mobile": (390, 844), "desktop": (1280, 800)}
PRIVATE = ["overview", "sounds", "sensors", "channels", "household"]
PUBLIC = ["login", "signup"]


def seed(live):
    """A household with three online sensors, one offline, one waiting to pair, some history."""
    import httpx

    owner = _member(live, household="Maple House", timezone="Europe/London")
    kitchen = _device(live, owner, "Kitchen")
    living = _device(live, owner, "Living room")
    hall = _device(live, owner, "Hallway")
    attic = _device(live, owner, "Attic")
    for d in (kitchen, living, hall, attic):
        _heartbeat(live, d)
    for d, kind in ((kitchen, "doorbell"), (living, "running_water"), (kitchen, "knocking"),
                    (hall, "doorbell"), (living, "doorbell")):  # fmt: skip
        _event(live, d, kind, 0.7)
    ids = []
    for d, kind in ((kitchen, "smoke_alarm"), (living, "glass_breaking")):
        event_id, alert_ids = _alerting_event(live, d, kind)
        ids += alert_ids
    _deliver(live, *ids)
    _go_offline(live, attic.id)
    httpx.post(  # a sensor still waiting to be paired
        f"{live.base_url}/devices/pairing-codes",
        json={"name": "Bedroom", "kind": "laptop"}, headers=owner.headers,
    ).raise_for_status()  # fmt: skip
    return owner, living


def main():
    from playwright.sync_api import expect, sync_playwright

    db.pytest_configure(None)  # same lock as the tests: never two runs on one database
    OUT.mkdir(parents=True, exist_ok=True)
    with run_live_server() as live, sync_playwright() as p:
        try:
            owner, living = seed(live)
            browser = p.chromium.launch(channel="chrome", headless=True)
            for size, (w, h) in SIZES.items():
                ctx = browser.new_context(
                    base_url=live.base_url, viewport={"width": w, "height": h},
                    color_scheme="light", locale="en-GB", timezone_id="Europe/London",
                )  # fmt: skip
                ctx.set_default_timeout(8000)
                for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
                    ctx.route(f"https://{host}/**", lambda route: route.abort())
                ctx.add_init_script(STUBS)
                page = ctx.new_page()
                for name in PUBLIC:
                    page.goto(f"/app/#/{name}")
                    expect(page.get_by_role("heading", level=1)).to_be_visible()
                    page.screenshot(path=OUT / f"{name}-{size}.png")
                page.evaluate("t => sessionStorage.setItem('ha.token', t)", owner.token)
                for name in PRIVATE:
                    page.goto(f"/app/#/{name}")
                    expect(page.get_by_role("heading", level=1)).to_be_visible()
                    page.wait_for_function("() => document.documentElement.dataset.live === 'live'")
                    if name == "overview":
                        expect(page.locator("section.events[aria-busy='false']")).to_have_count(1)
                    elif name == "sounds":
                        expect(page.locator(".snd-row").first).to_be_visible()
                    else:
                        expect(page.locator("main [data-ready='true']")).to_have_count(1)
                    page.screenshot(path=OUT / f"{name}-{size}.png", full_page=True)
                # a live alert takes over the page
                page.goto("/app/#/overview")
                page.wait_for_function("() => document.documentElement.dataset.live === 'live'")
                live.redis.flushall()  # forget the cooldown from the previous pass
                _, alert_ids = _alerting_event(live, living, "scream")
                _deliver(live, *alert_ids)
                expect(page.get_by_role("alertdialog")).to_be_visible()
                page.screenshot(path=OUT / f"alert-{size}.png")
                ctx.close()
            browser.close()
        finally:
            _run(_truncate_all())
    for f in sorted(OUT.glob("*.png")):
        print(f"{f.relative_to(ROOT)}  {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
