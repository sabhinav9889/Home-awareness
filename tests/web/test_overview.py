"""Overview + live events: real browser, real API, real /ws (fakeredis pub/sub)."""

import base64
import json
import re
import time
import types
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import fakeredis.aioredis
import httpx
import pytest
from playwright.sync_api import expect
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.devices.models import Device
from app.devices.offline import check_offline_devices
from tests.conftest import TEST_DATABASE_URL
from tests.web.conftest import _run

pytestmark = pytest.mark.web
PW = "correct-horse-1"
SESSION_ENDED = "Your session ended — please log in again."
XSS = "<img src=x onerror=alert(1)>"
AMBER = "rgb(180, 83, 9)"  # #B45309


# --- API-side helpers (the "other side": members and fake sensors) ---


def _member(live, household="Maple House", timezone="UTC"):
    email = f"u{uuid.uuid4().hex[:10]}@example.com"
    with httpx.Client(base_url=live.base_url) as c:
        r = c.post(
            "/auth/signup",
            json={
                "email": email, "password": PW, "household_name": household,
                "timezone": timezone,
            },
        )  # fmt: skip
        assert r.status_code == 201, r.text
        token = live.outbox[-1].body.split("token=")[1].split()[0]
        assert c.post("/auth/confirm", json={"token": token}).status_code == 204
        r = c.post("/auth/login", json={"email": email, "password": PW})
        assert r.status_code == 200, r.text
    jwt = r.json()["access_token"]
    claims = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
    return types.SimpleNamespace(
        token=jwt, headers={"Authorization": f"Bearer {jwt}"}, id=int(claims["sub"])
    )


def _device(live, member, name="Hall"):
    with httpx.Client(base_url=live.base_url) as c:
        r = c.post(
            "/devices/pairing-codes", json={"name": name, "kind": "pi"}, headers=member.headers
        )
        assert r.status_code == 201, r.text
        r = c.post("/device/pair", json={"code": r.json()["code"]})
        assert r.status_code == 201, r.text
        paired = r.json()
    return types.SimpleNamespace(id=paired["device_id"], key={"X-Device-Key": paired["device_key"]})


def _heartbeat(live, device):
    r = httpx.post(f"{live.base_url}/device/heartbeat", headers=device.key)
    assert r.status_code == 200, r.text


def _event(live, device, type_="doorbell", confidence=0.8):
    body = {
        "type": type_, "confidence": confidence,
        "occurred_at": datetime.now(UTC).isoformat(),
    }  # fmt: skip
    r = httpx.post(f"{live.base_url}/device/events", json=body, headers=device.key)
    assert r.status_code == 202, r.text
    return r.json()


def _go_offline(live, device_id):
    """Make the sensor stale and run the real offline check (it publishes device_offline)."""

    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        now = datetime.now(UTC)
        async with engine.begin() as conn:
            # older than FLEET_SILENCE_MAX, so the fleet-silence guard doesn't hold it back
            await conn.execute(
                update(Device)
                .where(Device.id == device_id)
                .values(last_seen_at=now - timedelta(minutes=11))
            )
        from sqlalchemy.ext.asyncio import AsyncSession

        redis = fakeredis.aioredis.FakeRedis(server=live.redis_server, decode_responses=True)
        async with AsyncSession(engine) as session:
            marked = await check_offline_devices(session, redis, now)
        await engine.dispose()
        assert marked == [device_id]

    _run(go())


# --- browser helpers ---


def _open(page, member):
    page.goto("/app/#/login")
    page.evaluate("t => sessionStorage.setItem('ha.token', t)", member.token)
    page.goto("/app/#/overview")
    _synced(page)


def _pill(page):
    return page.locator(".live-pill")


def _synced(page):
    """Live, and the catch-up fetch that follows every (re)connect has been merged."""
    expect(_pill(page)).to_have_text("Live")
    expect(page.locator("section.events[aria-busy='false']")).to_have_count(1)


def _rows(page):
    return page.get_by_role("table", name="Live events").locator("tbody tr")


def _row(page, text):
    return _rows(page).filter(has_text=text)


def _sensor(page, name):
    return page.locator(".sensor-card").filter(has_text=name)


# --- tests ---


def test_event_from_a_sensor_appears_without_reload(page, live):
    m = _member(live)
    hall = _device(live, m, "Hall")
    _heartbeat(live, hall)
    _open(page, m)
    expect(page.get_by_text("All quiet right now")).to_be_visible()
    expect(_rows(page)).to_have_count(0)
    page.evaluate("window.__sameDocument = true")
    _event(live, hall, "doorbell", 0.8)
    row = _row(page, "Doorbell")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Hall")
    expect(row).to_contain_text("0.80")
    expect(row.locator(".chip")).to_have_text("Logged")
    expect(page.locator(".count-events")).to_have_text("1")
    assert page.evaluate("window.__sameDocument") is True  # no reload happened
    expect(_sensor(page, "Hall")).to_contain_text("Listening")
    expect(_sensor(page, "Hall")).to_contain_text(re.compile(r"heartbeat \d+ s ago"))


def test_result_chips_alerted_and_merged(page, live):
    m = _member(live)
    hall, kitchen = _device(live, m, "Hall"), _device(live, m, "Kitchen")
    assert _event(live, hall, "glass_breaking", 0.9)["alerts"] == 1  # before the page loads
    _open(page, m)
    expect(_row(page, "Glass breaking").locator(".chip")).to_have_text("Alerted")
    assert _event(live, hall, "scream", 0.9)["alerts"] == 1  # live
    first = _event(live, kitchen, "scream", 0.7)
    assert first["merged"] is True
    expect(_row(page, "Kitchen").locator(".chip")).to_have_text("Merged")
    expect(_rows(page).filter(has_text="Scream").filter(has_text="Hall").locator(".chip"))\
        .to_have_text("Alerted")  # fmt: skip
    expect(page.locator(".count-alerts")).to_have_text("2")
    expect(page.locator(".count-events")).to_have_text("3")


def test_times_use_the_household_timezone(new_page, live):
    page = new_page(timezone_id="America/New_York")
    m = _member(live, timezone="Asia/Kolkata")
    hall = _device(live, m)
    _event(live, hall)
    received = httpx.get(f"{live.base_url}/events", headers=m.headers).json()[0]["received_at"]
    local = datetime.fromisoformat(received).astimezone(ZoneInfo("Asia/Kolkata"))
    _open(page, m)
    expect(_row(page, "Doorbell").locator("td").first).to_have_text(local.strftime("%H:%M:%S"))


def test_device_going_offline_turns_its_card_amber_and_back(page, live):
    m = _member(live)
    hall = _device(live, m, "Hall")
    _heartbeat(live, hall)
    _open(page, m)
    card = _sensor(page, "Hall")
    expect(card).to_contain_text("Listening")
    _go_offline(live, hall.id)
    expect(card).to_have_class(re.compile(r"\bsensor-offline\b"))
    expect(card).to_contain_text("Offline")
    expect(page.get_by_text("1 sensor offline")).to_be_visible()
    assert card.evaluate("e => getComputedStyle(e).borderTopColor") == AMBER
    _heartbeat(live, hall)  # back: device_online
    expect(card).to_contain_text("Listening")
    expect(card).not_to_have_class(re.compile(r"\bsensor-offline\b"))
    expect(page.get_by_text("All quiet right now")).to_be_visible()


def test_reconnect_after_a_dropped_socket_without_duplicates(page, live):
    # route_web_socket proxies the page's socket to the real server, so the test can drop
    # it from the browser's side without any test-only hook in the dashboard's code.
    sockets = []

    def proxy(ws):
        ws.connect_to_server()
        sockets.append(ws)

    page.route_web_socket(re.compile(r"/ws\?token="), proxy)
    console = []
    page.on("console", lambda msg: console.append(msg.text))
    m = _member(live)
    hall = _device(live, m)
    _event(live, hall, "doorbell")
    _open(page, m)
    expect(_rows(page)).to_have_count(1)
    assert len(sockets) == 1
    sockets[0].close(code=1012, reason="restart")
    expect(_pill(page)).to_have_text("Reconnecting…")
    _event(live, hall, "door_slam")  # while the socket is down: caught up on reconnect
    _synced(page)
    assert len(sockets) == 2
    expect(_row(page, "Door slam")).to_have_count(1)
    expect(_row(page, "Doorbell")).to_have_count(1)
    expect(_rows(page)).to_have_count(2)
    _event(live, hall, "knocking")  # the new socket delivers live again
    expect(_row(page, "knocking")).to_have_count(1)
    expect(_rows(page)).to_have_count(3)
    assert not any(m.token in line or "/ws" in line for line in console)


def test_server_disconnect_logs_out_with_message(page, live):
    m = _member(live)
    _open(page, m)
    live.redis.publish(f"live:member:{m.id}", json.dumps({"kind": "disconnect"}))  # -> 4403
    expect(page).to_have_url(re.compile(r"#/login"))
    expect(page.get_by_role("status")).to_have_text(SESSION_ENDED)
    assert page.evaluate("sessionStorage.getItem('ha.token')") is None


def test_leaving_the_overview_closes_the_socket(page, live):
    m = _member(live)
    with page.expect_websocket() as info:
        _open(page, m)
    ws = info.value
    opened = []
    page.on("websocket", lambda w: opened.append(w))
    with ws.expect_event("close"):
        page.get_by_role("button", name="Log out").click()
    expect(page).to_have_url(re.compile(r"#/login"))
    time.sleep(1.5)  # longer than the first reconnect delay
    assert opened == []


def test_device_name_is_text_not_html(page, live):
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    m = _member(live)
    evil = _device(live, m, XSS)
    _heartbeat(live, evil)
    _event(live, evil, "doorbell")
    _open(page, m)
    _event(live, evil, "door_slam")  # live path too
    expect(_sensor(page, XSS)).to_be_visible()
    expect(_row(page, "Doorbell")).to_contain_text(XSS)
    expect(_row(page, "Door slam")).to_contain_text(XSS)
    assert page.locator("img[src=x]").count() == 0
    assert dialogs == []


def test_events_table_scrolls_inside_its_box_at_phone_width(new_page, live):
    page = new_page(viewport={"width": 360, "height": 740})
    m = _member(live)
    hall = _device(live, m, "A sensor with a fairly long name")
    _heartbeat(live, hall)
    _event(live, hall)
    _open(page, m)
    expect(_rows(page)).to_have_count(1)
    box = page.locator(".table-scroll")
    assert box.evaluate("e => getComputedStyle(e).overflowX") == "auto"
    assert box.evaluate("e => e.scrollWidth > e.clientWidth")  # the table scrolls in its box
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_backoff_grows_when_the_server_drops_every_socket_at_once(page, live):
    # Each socket "opens" (the route accepts it) and is closed at once. The backoff must not
    # reset on such a short-lived open: retries come 1, 2, 4 s apart, not every second.
    opened, sockets = [], []

    def accept(ws):  # closed from the test thread below (closing inside the handler stalls)
        opened.append(time.monotonic())
        sockets.append(ws)

    page.route_web_socket(re.compile(r"/ws\?token="), accept)
    events_calls = []
    page.on("request", lambda r: "/events?" in r.url and events_calls.append(r.url))
    m = _member(live)
    page.goto("/app/#/login")
    page.evaluate("t => sessionStorage.setItem('ha.token', t)", m.token)
    page.goto("/app/#/overview")
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    deadline = time.monotonic() + 9
    while len(opened) < 4 and time.monotonic() < deadline:
        while sockets:
            sockets.pop().close(code=1011, reason="boom")
        page.wait_for_timeout(50)
    assert len(opened) >= 4, opened
    for ws in sockets:
        ws.close(code=1011, reason="boom")
    gaps = [b - a for a, b in zip(opened, opened[1:], strict=False)][:3]
    assert gaps[0] >= 0.9 and gaps[1] >= 1.9 and gaps[2] >= 3.9, gaps
    expect(_pill(page)).to_have_text("Reconnecting…")
    # one initial load plus at most one catch-up per open, never a storm
    assert len(events_calls) <= len(opened) + 1, events_calls


def test_sensor_cards_refresh_from_the_server_every_30_s(page, live):
    page.clock.install()
    m = _member(live)
    hall = _device(live, m, "Hall")  # pending: its first heartbeat publishes nothing live
    _open(page, m)
    card = _sensor(page, "Hall")
    expect(card).to_contain_text("Waiting for its first heartbeat")
    _heartbeat(live, hall)
    with page.expect_response(re.compile(r"/devices$")):
        page.clock.fast_forward(31_000)
    expect(card).to_contain_text("Listening")
