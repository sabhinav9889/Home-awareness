"""Alert screen: real browser, real API, real /ws, real alert delivery (fakeredis pub/sub).

The web harness records enqueued alert ids instead of running a worker; `_deliver` runs the
real `deliver_alert` for them, so the browser gets exactly the message production sends.
"""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import fakeredis.aioredis
import httpx
import pytest
from playwright.sync_api import expect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.alerts.delivery import deliver_alert
from tests.conftest import TEST_DATABASE_URL
from tests.web.conftest import _run
from tests.web.test_overview import XSS, _device, _member, _synced

pytestmark = pytest.mark.web
HANDLED_BY_ME = "I'm on it"

# Stubs for the things a headless test can't observe: Web Audio (records oscillator
# start/stop instead of making sound), page visibility (window.__hidden) and Notification
# (permission "granted", records every notification and every permission request).
STUBS = """
(() => {
  const audio = window.__audio = { contexts: 0, started: 0, stopped: 0, closed: 0 };
  class Param {
    constructor() { this.value = 0; }
    setValueAtTime(v) { this.value = v; }
    linearRampToValueAtTime() {}
    exponentialRampToValueAtTime() {}
    cancelScheduledValues() {}
  }
  class Node { connect(n) { return n; } disconnect() {} }
  class Osc extends Node {
    constructor() { super(); this.frequency = new Param(); this.type = "sine"; }
    start() { audio.started += 1; }
    stop() { audio.stopped += 1; }
  }
  class Gain extends Node { constructor() { super(); this.gain = new Param(); } }
  class Ctx {
    constructor() { audio.contexts += 1; this.currentTime = 0; this.state = "running";
      this.destination = new Node(); }
    createOscillator() { return new Osc(); }
    createGain() { return new Gain(); }
    resume() { return Promise.resolve(); }
    close() { audio.closed += 1; this.state = "closed"; return Promise.resolve(); }
  }
  window.AudioContext = Ctx;
  window.webkitAudioContext = Ctx;

  window.__hidden = false;
  Object.defineProperty(Document.prototype, "visibilityState",
    { configurable: true, get() { return window.__hidden ? "hidden" : "visible"; } });
  Object.defineProperty(Document.prototype, "hidden",
    { configurable: true, get() { return window.__hidden; } });

  window.__notes = [];
  window.__permissionAsked = 0;
  window.__focused = 0;
  class FakeNotification {
    constructor(title, opts) { this.title = title; this.opts = opts || {}; this.closed = false;
      window.__notes.push(this); }
    close() { this.closed = true; }
    static requestPermission() { window.__permissionAsked += 1; return Promise.resolve("granted"); }
  }
  FakeNotification.permission = "granted";
  window.Notification = FakeNotification;
  const focus = window.focus.bind(window);
  window.focus = () => { window.__focused += 1; focus(); };
})();
"""


# --- API side ---


def _deliver(live, *alert_ids):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        redis = fakeredis.aioredis.FakeRedis(server=live.redis_server, decode_responses=True)
        async with httpx.AsyncClient() as http:
            for alert_id in alert_ids:
                await deliver_alert(alert_id, session_factory=factory, redis=redis, http=http)
        await engine.dispose()

    _run(go())


def _test_alert(live, member, deliver=True):
    r = httpx.post(f"{live.base_url}/me/test-alert", headers=member.headers)
    assert r.status_code == 202, r.text
    alert_id = r.json()["alert_id"]
    if deliver:
        _deliver(live, alert_id)
    return alert_id


def _event(live, device, type_="glass_breaking"):
    """An event that alerts; returns (event_id, the new alert ids), not yet delivered."""
    before = len(live.jobs.enqueued)
    now = datetime.now().astimezone().isoformat()
    body = {"type": type_, "confidence": 0.9, "occurred_at": now}
    r = httpx.post(f"{live.base_url}/device/events", json=body, headers=device.key)
    assert r.status_code == 202, r.text
    assert r.json()["alerts"] >= 1, r.json()
    return r.json()["event_id"], live.jobs.enqueued[before:]


def _my_alert(live, member, event_id):
    r = httpx.get(f"{live.base_url}/alerts", headers=member.headers)
    assert r.status_code == 200, r.text
    return next(a for a in r.json() if a["event_id"] == event_id)


def _household(live, member):
    return httpx.get(f"{live.base_url}/households/me", headers=member.headers).json()


def _second_member(live, owner):
    r = httpx.post(f"{live.base_url}/households/me/invites", headers=owner.headers)
    assert r.status_code == 201, r.text
    return _member(live, invite_token=r.json()["invite_url"].split("invite=")[1])


def _prefs(live, member, **changes):
    body = {"popup": True, "alarm": False, "telegram": False, "ntfy_topic": None, **changes}
    r = httpx.put(f"{live.base_url}/me/alert-preferences", json=body, headers=member.headers)
    assert r.status_code == 200, r.text


def _publish_ack(live, household_id, event_id, by_member_id, by_email):
    live.redis.publish(
        f"live:household:{household_id}",
        json.dumps({
            "kind": "alert_ack", "event_id": event_id, "by_member_id": by_member_id,
            "by_email": by_email, "at": datetime.now().astimezone().isoformat(),
        }),
    )  # fmt: skip


# --- browser side ---


def _page(new_page, **options):
    page = new_page(**options)
    page.add_init_script(STUBS)
    return page


def _login(page, member, hash_="#/overview"):
    page.goto("/app/#/login")
    page.evaluate("t => sessionStorage.setItem('ha.token', t)", member.token)
    page.goto(f"/app/{hash_}")
    if hash_ == "#/overview":
        _synced(page)
    else:
        expect(page.get_by_role("heading", level=1)).to_be_visible()
    _wait_live(page)


def _wait_live(page):
    """The app-level socket is open (it serves every view, not just the overview)."""
    page.wait_for_function("document.documentElement.dataset.live === 'live'")


def _screen(page):
    return page.get_by_role("alertdialog")


def _button(page, name):
    return _screen(page).get_by_role("button", name=name, exact=True)


def _beeping(page):
    return page.evaluate("window.__audio.started > window.__audio.stopped")


def _set_hidden(page, hidden):
    page.evaluate(
        "h => { window.__hidden = h; document.dispatchEvent(new Event('visibilitychange')); }",
        hidden,
    )


# --- tests ---


def test_test_alert_takes_over_the_overview_and_the_sounds_view(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _login(page, m)
    expect(_screen(page)).to_have_count(0)
    _test_alert(live, m)
    screen = _screen(page)
    expect(screen).to_be_visible()
    expect(screen).to_have_attribute("aria-live", "assertive")
    expect(screen.get_by_role("heading")).to_have_text("Test alert")
    for name in (HANDLED_BY_ME, "Event details", "Dismiss"):
        expect(_button(page, name)).to_be_visible()
    expect(screen).to_be_focused()  # the dialog, not a button: a stray key can't act
    assert page.evaluate("document.getElementById('root').inert") is True
    _button(page, "Dismiss").click()
    expect(screen).to_have_count(0)
    assert page.evaluate("document.getElementById('root').inert") is False

    page.goto("/app/#/sounds")  # a fresh load: the app opens its own socket on any view
    expect(page.get_by_role("heading", name="Sounds & rules")).to_be_visible()
    _wait_live(page)
    _test_alert(live, m)
    expect(_screen(page)).to_be_visible()
    expect(_screen(page).get_by_role("heading")).to_have_text("Test alert")


def test_alert_survives_navigation_and_one_socket_serves_every_view(new_page, live):
    page = _page(new_page)
    sockets = []
    page.on("websocket", lambda ws: sockets.append(ws))
    m = _member(live)
    _login(page, m)
    page.get_by_role("link", name="Sounds & rules").click()
    expect(page.get_by_role("heading", name="Sounds & rules")).to_be_visible()
    page.get_by_role("link", name="Overview").click()
    _synced(page)
    page.get_by_role("link", name="Sounds & rules").click()
    expect(page.get_by_role("heading", name="Sounds & rules")).to_be_visible()
    _test_alert(live, m)
    expect(_screen(page)).to_be_visible()
    assert len(sockets) == 1  # navigating doesn't reopen the socket
    _button(page, "Event details").click()
    expect(page).to_have_url(re.compile(r"#/overview$"))
    expect(_screen(page)).to_have_count(0)
    _synced(page)


def test_im_on_it_posts_the_ack_and_closes(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _login(page, m)
    alert_id = _test_alert(live, m)
    expect(_screen(page)).to_be_visible()
    with page.expect_response(re.compile(rf"/alerts/{alert_id}/ack$")) as info:
        _button(page, HANDLED_BY_ME).click()
    assert info.value.status == 200
    assert info.value.request.method == "POST"
    expect(_screen(page)).to_have_count(0)
    # the server agrees: a second ack is "already handled" by me
    r = httpx.post(f"{live.base_url}/alerts/{alert_id}/ack", headers=m.headers)
    assert r.status_code == 409 and r.json()["by"] == m.email


def test_other_member_sees_who_is_handling_it_and_the_beep_stops(new_page, live):
    owner = _member(live)
    member = _second_member(live, owner)
    _prefs(live, member, alarm=True)
    hall = _device(live, owner, "Hall")
    owner_page, member_page = _page(new_page), _page(new_page)
    _login(owner_page, owner)
    _login(member_page, member)
    event_id, alert_ids = _event(live, hall, "glass_breaking")
    assert len(alert_ids) == 2
    _deliver(live, *alert_ids)
    for p in (owner_page, member_page):
        expect(_screen(p).get_by_role("heading")).to_have_text("Glass breaking")
        expect(_screen(p)).to_contain_text("Hall")
    assert _beeping(member_page)
    _button(owner_page, HANDLED_BY_ME).click()
    expect(_screen(owner_page)).to_have_count(0)
    screen = _screen(member_page)
    expect(screen).to_contain_text(f"{owner.email} is handling it")
    expect(screen.get_by_role("heading")).to_have_text("Glass breaking")
    expect(_button(member_page, HANDLED_BY_ME)).to_have_count(0)
    expect(screen).to_be_focused()
    assert not _beeping(member_page)
    _button(member_page, "Close").click()
    expect(screen).to_have_count(0)
    assert _my_alert(live, member, event_id)["event_id"] == event_id


def test_own_ack_in_another_tab_closes_this_one(new_page, live):
    m = _member(live)
    hall = _device(live, m, "Hall")
    tab1, tab2 = _page(new_page), _page(new_page)
    _login(tab1, m)
    _login(tab2, m)
    _event_id, alert_ids = _event(live, hall, "glass_breaking")
    _deliver(live, *alert_ids)
    expect(_screen(tab1)).to_be_visible()
    expect(_screen(tab2)).to_be_visible()
    _button(tab1, HANDLED_BY_ME).click()
    expect(_screen(tab1)).to_have_count(0)
    expect(_screen(tab2)).to_have_count(0)  # my own ack: no "you are handling it" screen


def test_already_handled_shows_who(new_page, live):
    owner = _member(live)
    member = _second_member(live, owner)
    hall = _device(live, owner, "Hall")
    page = _page(new_page)
    _login(page, member)
    event_id, alert_ids = _event(live, hall, "glass_breaking")
    owner_alert = _my_alert(live, owner, event_id)["id"]
    member_alert = _my_alert(live, member, event_id)["id"]
    # the owner acks before the member's alert arrives: its alert_ack is for an untracked event
    r = httpx.post(f"{live.base_url}/alerts/{owner_alert}/ack", headers=owner.headers)
    assert r.status_code == 200, r.text
    _deliver(live, member_alert)
    expect(_button(page, HANDLED_BY_ME)).to_be_visible()
    with page.expect_response(re.compile(rf"/alerts/{member_alert}/ack$")) as info:
        _button(page, HANDLED_BY_ME).click()
    assert info.value.status == 409
    expect(_screen(page)).to_contain_text(f"{owner.email} is already handling it")
    expect(_button(page, HANDLED_BY_ME)).to_have_count(0)
    _button(page, "Close").click()
    expect(_screen(page)).to_have_count(0)


def test_network_error_keeps_the_screen_and_retry_works(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _login(page, m)
    _test_alert(live, m)
    page.route(re.compile(r"/alerts/\d+/ack$"), lambda route: route.abort())
    _button(page, HANDLED_BY_ME).click()
    expect(_screen(page)).to_contain_text("Couldn't reach the server — try again")
    expect(_button(page, HANDLED_BY_ME)).to_be_enabled()
    page.unroute(re.compile(r"/alerts/\d+/ack$"))
    _button(page, HANDLED_BY_ME).click()
    expect(_screen(page)).to_have_count(0)


def test_two_alerts_queue_newest_first(new_page, live):
    page = _page(new_page, viewport={"width": 360, "height": 740})
    m = _member(live)
    hall, evil = _device(live, m, "Hall"), _device(live, m, XSS)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _login(page, m)
    _, first = _event(live, hall, "glass_breaking")
    _, second = _event(live, evil, "scream")
    _deliver(live, *first)
    expect(_screen(page).get_by_role("heading")).to_have_text("Glass breaking")
    expect(page.locator(".alert-more")).to_have_count(0)
    _deliver(live, *second)
    screen = _screen(page)
    expect(screen.get_by_role("heading")).to_have_text("Scream")
    expect(screen).to_contain_text(XSS)  # a device name is text, never markup
    expect(page.locator(".alert-more")).to_have_text("+1 more")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator("img[src=x]").count() == 0
    _button(page, "Dismiss").click()
    expect(screen.get_by_role("heading")).to_have_text("Glass breaking")
    expect(page.locator(".alert-more")).to_have_count(0)
    expect(screen).to_be_focused()
    _button(page, "Dismiss").click()
    expect(screen).to_have_count(0)
    assert dialogs == []


def test_acks_for_other_events_leave_the_screen_alone(new_page, live):
    owner = _member(live)
    other = _second_member(live, owner)
    hall = _device(live, owner, "Hall")
    page = _page(new_page)
    frames = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda f: frames.append(f)))
    _login(page, owner)
    event_id, alert_ids = _event(live, hall, "glass_breaking")
    _deliver(live, *alert_ids)
    expect(_button(page, HANDLED_BY_ME)).to_be_visible()
    hid = _household(live, owner)["id"]
    seen = len(frames)
    _publish_ack(live, hid, event_id + 1000, other.id, "stranger@example.com")
    _publish_ack(live, hid, None, other.id, "stranger@example.com")  # a test alert's ack
    for _ in range(40):  # both reached the browser
        if len(frames) >= seen + 2:
            break
        page.wait_for_timeout(50)
    assert len(frames) >= seen + 2
    page.wait_for_timeout(200)  # and the app has handled them
    expect(_button(page, HANDLED_BY_ME)).to_be_visible()
    expect(_screen(page)).not_to_contain_text("stranger")
    _publish_ack(live, hid, event_id, other.id, other.email)  # the tracked one does
    expect(_screen(page)).to_contain_text(f"{other.email} is handling it")


def test_alert_time_uses_the_household_timezone(new_page, live):
    page = _page(new_page, timezone_id="America/New_York")
    m = _member(live, timezone="Asia/Kolkata")
    _login(page, m)
    alert_id = _test_alert(live, m)
    rows = httpx.get(f"{live.base_url}/alerts", headers=m.headers).json()
    created = next(a for a in rows if a["id"] == alert_id)["created_at"]
    local = datetime.fromisoformat(created).astimezone(ZoneInfo("Asia/Kolkata"))
    expect(_screen(page)).to_contain_text(local.strftime("%H:%M:%S"))


def test_beep_plays_only_with_alarm_and_stops_on_a_button(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _login(page, m)
    _test_alert(live, m)  # alarm off by default
    expect(_screen(page)).to_be_visible()
    assert page.evaluate("window.__audio.started") == 0
    _button(page, "Dismiss").click()
    _prefs(live, m, alarm=True)
    _test_alert(live, m)
    expect(_screen(page)).to_be_visible()
    page.wait_for_function("window.__audio.started > 0")
    assert _beeping(page)
    _button(page, HANDLED_BY_ME).click()
    expect(_screen(page)).to_have_count(0)
    assert not _beeping(page)
    assert page.evaluate("window.__audio.closed") == page.evaluate("window.__audio.contexts")


def test_beep_stops_after_30_seconds(new_page, live):
    page = _page(new_page)
    page.clock.install()
    m = _member(live)
    _prefs(live, m, alarm=True)
    _login(page, m)
    _test_alert(live, m)
    page.wait_for_function("window.__audio.started > 0")
    assert _beeping(page)
    page.clock.fast_forward(29_000)
    assert _beeping(page)
    page.clock.fast_forward(2_000)
    assert not _beeping(page)
    expect(_button(page, HANDLED_BY_ME)).to_be_visible()  # the screen itself stays


def test_hidden_tab_gets_a_notification_and_no_beep(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _prefs(live, m, alarm=True)
    _login(page, m)
    _set_hidden(page, True)
    _test_alert(live, m)
    page.wait_for_function("window.__notes.length === 1")
    note = page.evaluate("({title: __notes[0].title, body: __notes[0].opts.body})")
    assert note["title"] == "Test alert"
    assert page.evaluate("window.__audio.started") == 0  # no beep in a hidden tab
    expect(_screen(page)).to_have_count(1)  # the screen waits for the person to come back
    page.evaluate("__notes[0].onclick && __notes[0].onclick(new Event('click'))")
    assert page.evaluate("window.__focused") >= 1
    assert page.evaluate("window.__permissionAsked") == 0  # never asked from here


def test_visible_tab_gets_no_notification_and_hiding_stops_the_beep(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _prefs(live, m, alarm=True)
    _login(page, m)
    _test_alert(live, m)
    page.wait_for_function("window.__audio.started > 0")
    assert page.evaluate("window.__notes.length") == 0
    _set_hidden(page, True)
    assert not _beeping(page)
    assert page.evaluate("window.__permissionAsked") == 0


def test_no_notification_without_permission(new_page, live):
    page = _page(new_page)
    page.add_init_script("Notification.permission = 'default'")
    m = _member(live)
    _login(page, m)
    _set_hidden(page, True)
    _test_alert(live, m)
    expect(_screen(page)).to_have_count(1)
    assert page.evaluate("window.__notes.length") == 0
    assert page.evaluate("window.__permissionAsked") == 0


def test_logout_clears_the_alert_screen(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _login(page, m)
    _test_alert(live, m)
    expect(_screen(page)).to_be_visible()
    page.evaluate("sessionStorage.removeItem('ha.token'); location.hash = '#/login'")
    expect(page).to_have_url(re.compile(r"#/login"))
    expect(_screen(page)).to_have_count(0)


# Records every alert action button as it is inserted: (heading, action, disabled at insert).
RECORD_BUTTONS = """
window.__buttons = [];
new MutationObserver((records) => {
  for (const r of records) for (const n of r.addedNodes) {
    if (!(n instanceof Element)) continue;
    for (const b of n.querySelectorAll("[data-action]")) {
      const h = document.getElementById("alert-title");
      window.__buttons.push([h ? h.textContent : "", b.dataset.action, b.disabled]);
    }
  }
}).observe(document, { childList: true, subtree: true });
"""


def test_stray_keys_never_ack_or_dismiss(new_page, live):
    page = _page(new_page)
    m = _member(live)
    _login(page, m, "#/sensors")
    acks = []
    page.on("request", lambda r: r.method == "POST" and r.url.endswith("/ack") and acks.append(r))
    box = page.get_by_label("Sensor name")
    box.click()
    page.keyboard.type("Kit")  # someone typing when the alert arrives
    _test_alert(live, m)
    screen = _screen(page)
    expect(screen).to_be_focused()
    for key in ("Space", "Enter", "Escape"):
        page.keyboard.press(key)
    expect(_button(page, HANDLED_BY_ME)).to_be_enabled()  # past the 600 ms guard
    for key in ("Space", "Enter", "Escape", "Escape"):
        page.keyboard.press(key)
    page.evaluate("new Promise(requestAnimationFrame)")
    expect(screen).to_be_visible()
    expect(screen).to_be_focused()
    assert acks == []
    # a deliberate Tab to "I'm on it" and Enter does ack
    page.keyboard.press("Tab")
    expect(_button(page, HANDLED_BY_ME)).to_be_focused()
    with page.expect_response(re.compile(r"/alerts/\d+/ack$")) as info:
        page.keyboard.press("Enter")
    assert info.value.status == 200
    expect(screen).to_have_count(0)
    expect(box).to_be_focused()  # back where the person was
    expect(box).to_have_value("Kit")


def test_buttons_are_guarded_for_600ms_whenever_the_shown_alert_changes(new_page, live):
    page = _page(new_page)
    page.add_init_script(RECORD_BUTTONS)
    m = _member(live)
    hall = _device(live, m, "Hall")
    _login(page, m)
    _, first = _event(live, hall, "glass_breaking")
    _deliver(live, *first)
    expect(_button(page, HANDLED_BY_ME)).to_be_enabled()
    _, second = _event(live, hall, "scream")
    _deliver(live, *second)
    expect(_screen(page).get_by_role("heading")).to_have_text("Scream")
    expect(_button(page, HANDLED_BY_ME)).to_be_enabled()
    log = page.evaluate("window.__buttons")
    for heading in ("Glass breaking", "Scream"):
        firsts = {}
        for h, action, disabled in log:
            if h == heading:
                firsts.setdefault(action, disabled)
        assert firsts == {"ack": True, "details": True, "dismiss": True}, (heading, log)
    # Dismiss with a deliberate click still works
    _button(page, "Dismiss").click()
    expect(_screen(page).get_by_role("heading")).to_have_text("Glass breaking")
