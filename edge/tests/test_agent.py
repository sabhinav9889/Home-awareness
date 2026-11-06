import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
import respx

from ha_edge.agent import EXIT_RELOGIN, ConnectionClosed, run_agent
from ha_edge.config import AgentConfig

TOKEN = "jwt-secret-token-abc"
CFG = AgentConfig(base_url="http://srv", email="me@example.com", access_token=TOKEN, member_id=1)
AT = "2026-11-07T12:34:00+00:00"
HHMM = datetime(2026, 11, 7, 12, 34, tzinfo=UTC).astimezone().strftime("%H:%M")
ALERT = {
    "kind": "alert",
    "alert_id": 7,
    "event_id": 3,
    "sound_key": "scream",
    "label": "Scream",
    "device_name": "Kitchen laptop",
    "at": AT,
    "popup": True,
    "alarm": True,
}
HANG = "hang"


class FakeConn:
    def __init__(self, items, end, server):
        self.items, self.end, self.server = items, end, server

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self.items:
            if isinstance(item, asyncio.Event):
                await item.wait()
            else:
                yield json.dumps(item) if isinstance(item, dict) else item
        if self.end == HANG:
            await asyncio.Event().wait()
        if isinstance(self.end, BaseException):
            raise self.end
        raise ConnectionClosed(self.end)


class FakeServer:
    """Each connect() takes the next session: (messages, end, seconds the link stays up).

    `end` is a close code, an exception, or HANG. With no sessions left, connect hangs.
    """

    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.urls = []
        self.now = 0.0

    def clock(self):
        return self.now

    @asynccontextmanager
    async def connect(self, url):
        self.urls.append(url)
        if not self.sessions:
            await asyncio.Event().wait()
        items, end, *up = self.sessions.pop(0)
        if isinstance(items, BaseException):  # handshake failure
            raise items
        try:
            yield FakeConn(items, end, self)
        finally:
            self.now += up[0] if up else 0.0


class FakeNotifier:
    def __init__(self, answers=None):
        self.popups = []
        self.answers = answers or {}

    async def popup(self, title, body, actions):
        self.popups.append((title, body, list(actions)))
        ans = self.answers.get(title)
        if isinstance(ans, asyncio.Future):
            return await ans
        return ans


class FakeAlarm:
    def __init__(self, fail=None):
        self.plays = 0
        self.stops = 0
        self.playing = False
        self.fail = fail
        self.log = []

    async def play(self):
        self.log.append("play")
        if self.fail:
            raise self.fail
        self.plays += 1
        self.playing = True

    def stop(self):
        self.log.append("stop")
        self.stops += 1
        self.playing = False


async def no_sleep(s):
    await asyncio.sleep(0)


async def _until(cond):
    async with asyncio.timeout(2):
        while not cond():
            await asyncio.sleep(0)


async def _run(
    server, notifier, alarm, *, stop=None, sleep=no_sleep, rand=lambda: 0.5, http=None, cfg=CFG
):
    own = http is None
    http = http or httpx.AsyncClient()
    try:
        async with asyncio.timeout(5):
            return await run_agent(
                cfg,
                connect=server.connect,
                notifier=notifier,
                alarm=alarm,
                clock=server.clock,
                http=http,
                sleep=sleep,
                rand=rand,
                stop=stop or asyncio.Event(),
            )
    finally:
        if own:
            await http.aclose()


def _start(server, notifier, alarm, **kw):
    stop = asyncio.Event()
    task = asyncio.create_task(_run(server, notifier, alarm, stop=stop, **kw))
    return stop, task


TITLE = "Scream — Kitchen laptop"


async def test_alert_with_alarm_shows_popup_and_starts_alarm(caplog):
    caplog.set_level(logging.DEBUG)
    server = FakeServer(([ALERT], HANG))
    notifier, alarm = FakeNotifier(), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: notifier.popups)
    assert notifier.popups[0] == (TITLE, f"{HHMM} · Home Awareness", ["I'm on it", "Stop alarm"])
    assert alarm.plays == 1 and alarm.playing
    assert server.urls == [f"http://srv/ws?token={TOKEN}"]
    stop.set()
    assert await task == 0
    assert not alarm.playing  # stop always silences the alarm
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


async def test_alert_without_alarm_flag_does_not_play():
    server = FakeServer(([dict(ALERT, alarm=False)], HANG))
    notifier, alarm = FakeNotifier(), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: notifier.popups)
    assert alarm.plays == 0
    stop.set()
    await task


async def test_stop_alarm_action_stops_it():
    server = FakeServer(([ALERT], HANG))
    notifier, alarm = FakeNotifier({TITLE: "Stop alarm"}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.plays and not alarm.playing)
    stop.set()
    await task


@respx.mock
async def test_im_on_it_posts_ack_and_stops_alarm():
    route = respx.post("http://srv/alerts/7/ack").mock(
        return_value=httpx.Response(200, json={"alert_id": 7})
    )
    server = FakeServer(([ALERT], HANG))
    notifier, alarm = FakeNotifier({TITLE: "I'm on it"}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: route.called)
    assert route.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert alarm.plays == 1 and not alarm.playing
    stop.set()
    await task


@respx.mock
async def test_im_on_it_when_someone_else_won_says_who():
    respx.post("http://srv/alerts/7/ack").mock(
        return_value=httpx.Response(409, json={"detail": "already handled", "by": "bob@x.org"})
    )
    server = FakeServer(([ALERT], HANG))
    notifier, alarm = FakeNotifier({TITLE: "I'm on it"}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: len(notifier.popups) == 2)
    assert notifier.popups[1][0] == "bob@x.org is already handling it"
    assert not alarm.playing
    stop.set()
    await task


@respx.mock
async def test_ack_network_error_is_reported_not_fatal():
    respx.post("http://srv/alerts/7/ack").mock(side_effect=httpx.ConnectError("down"))
    server = FakeServer(([ALERT], HANG))
    notifier, alarm = FakeNotifier({TITLE: "I'm on it"}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: len(notifier.popups) == 2)
    assert "Could not" in notifier.popups[1][0]
    assert not alarm.playing
    assert not task.done()
    stop.set()
    assert await task == 0


async def test_ack_from_other_member_stops_alarm_and_shows_who():
    gate = asyncio.Event()
    ack = {
        "kind": "alert_ack",
        "event_id": 3,
        "by_member_id": 9,
        "by_email": "bob@x.org",
        "at": AT,
    }
    server = FakeServer(([ALERT, gate, ack], HANG))
    pending = asyncio.get_running_loop().create_future()
    notifier, alarm = FakeNotifier({TITLE: pending}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.playing)
    gate.set()
    await _until(lambda: not alarm.playing)
    await _until(lambda: len(notifier.popups) == 2)
    assert notifier.popups[1][0] == "bob@x.org is handling it"
    stop.set()
    assert await task == 0


async def test_own_ack_from_elsewhere_stops_alarm_without_popup():
    gate = asyncio.Event()
    ack = {"kind": "alert_ack", "event_id": 3, "by_member_id": 1, "by_email": "ME@x.org", "at": AT}
    server = FakeServer(([ALERT, gate, ack, MARK], HANG))
    notifier, alarm = FakeNotifier({TITLE: asyncio.get_running_loop().create_future()}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.playing)
    gate.set()
    await _until(lambda: not alarm.playing)
    await _until_marked(notifier)  # the ack was fully handled
    assert len(_real(notifier)) == 1
    stop.set()
    await task


async def test_device_offline_popup_and_junk_ignored():
    msgs = [
        "not json",
        {"kind": "event", "type": "doorbell"},
        {"kind": "device_online", "device_id": 1, "name": "Hall"},
        {"kind": "device_offline", "device_id": 1, "name": "Hall"},
    ]
    server = FakeServer((msgs, HANG))
    notifier, alarm = FakeNotifier(), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: notifier.popups)
    assert notifier.popups == [("Sensor offline: Hall", "Home Awareness", [])]
    stop.set()
    await task


@pytest.mark.parametrize("code", [4401, 4403])
async def test_auth_close_asks_to_log_in_again_without_reconnect(code):
    slept = []

    async def sleep(s):
        slept.append(s)

    server = FakeServer(([], code), ([], HANG))
    notifier, alarm = FakeNotifier(), FakeAlarm()
    assert await _run(server, notifier, alarm, sleep=sleep) == EXIT_RELOGIN == 6
    assert len(server.urls) == 1 and slept == []
    assert notifier.popups[0][0] == "Please log in again: ha-agent login"


async def test_generic_drops_reconnect_with_exponential_backoff():
    slept = []

    async def sleep(s):
        slept.append(s)

    sessions = [([], 1006), ([], 1011), (httpx.ConnectError("refused"), None)]
    sessions += [([], 1006)] * 6
    server = FakeServer(*sessions)
    notifier, alarm = FakeNotifier(), FakeAlarm()
    stop, task = _start(server, notifier, alarm, sleep=sleep)
    await _until(lambda: len(slept) == 9)
    assert slept == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    stop.set()
    assert await task == 0


async def test_backoff_jitter_is_within_20_percent():
    for r, factor in ((0.0, 0.8), (1.0, 1.2)):
        slept = []

        async def sleep(s, slept=slept):
            slept.append(s)

        server = FakeServer(([], 1006), ([], 1006))
        stop, task = _start(server, FakeNotifier(), FakeAlarm(), sleep=sleep, rand=lambda r=r: r)
        await _until(lambda slept=slept: len(slept) == 2)
        assert slept == [pytest.approx(1 * factor), pytest.approx(2 * factor)]
        stop.set()
        await task


async def test_backoff_resets_after_connection_stays_up_60s():
    slept = []

    async def sleep(s):
        slept.append(s)

    server = FakeServer(([], 1006, 1), ([], 1006, 1), ([], 1006, 61), ([], 1006, 1))
    stop, task = _start(server, FakeNotifier(), FakeAlarm(), sleep=sleep)
    await _until(lambda: len(slept) == 4)
    assert slept == [1, 2, 1, 2]
    stop.set()
    await task


async def test_stop_during_backoff_exits_promptly():
    async def slow_sleep(s):
        await asyncio.sleep(3600)

    server = FakeServer(([], 1006))
    alarm = FakeAlarm()
    stop, task = _start(server, FakeNotifier(), alarm, sleep=slow_sleep)
    await _until(lambda: server.urls)
    await asyncio.sleep(0)
    stop.set()
    assert await task == 0
    assert alarm.stops >= 1


async def test_errors_never_log_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    slept = []

    async def sleep(s):
        slept.append(s)

    err = httpx.ConnectError(f"cannot reach http://srv/ws?token={TOKEN}")
    server = FakeServer((err, None))
    stop, task = _start(server, FakeNotifier(), FakeAlarm(), sleep=sleep)
    await _until(lambda: slept)
    stop.set()
    await task
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in text
    assert "http://srv" in text


# --- ws_connector over a session that, like httpx-ws, runs inside a TaskGroup ----------


class FakeWs:
    def __init__(self, texts, exc):
        self.texts, self.exc = list(texts), exc

    async def receive_text(self):
        if self.texts:
            return self.texts.pop(0)
        raise self.exc


def _fake_aconnect(texts, exc):
    @asynccontextmanager
    async def aconnect(url, client):
        async with asyncio.TaskGroup() as tg:  # httpx-ws wraps body errors in a group
            tg.create_task(asyncio.sleep(0))
            yield FakeWs(texts, exc)

    return aconnect


async def _drain(connect):
    got = []
    async with connect("http://srv/ws?token=x") as messages:
        async for m in messages:
            got.append(m)
    return got


@pytest.mark.parametrize("code", [4401, 4403, 1011])
async def test_ws_connector_unwraps_close_code_from_task_group(code):
    from httpx_ws import WebSocketDisconnect

    from ha_edge.agent import ws_connector

    connect = ws_connector(None, aconnect=_fake_aconnect(["hi"], WebSocketDisconnect(code)))
    with pytest.raises(ConnectionClosed) as e:
        await _drain(connect)
    assert e.value.code == code


async def test_ws_connector_network_error_is_a_close_without_code():
    from httpx_ws import WebSocketNetworkError

    from ha_edge.agent import ws_connector

    connect = ws_connector(None, aconnect=_fake_aconnect([], WebSocketNetworkError()))
    with pytest.raises(ConnectionClosed) as e:
        await _drain(connect)
    assert e.value.code is None


async def test_auth_close_through_task_group_still_exits_6():
    from httpx_ws import WebSocketDisconnect

    from ha_edge.agent import ws_connector

    notifier = FakeNotifier()
    async with httpx.AsyncClient() as http:
        rc = await run_agent(
            CFG,
            connect=ws_connector(http, aconnect=_fake_aconnect([], WebSocketDisconnect(4401))),
            notifier=notifier,
            alarm=FakeAlarm(),
            clock=lambda: 0.0,
            http=http,
            sleep=no_sleep,
            stop=asyncio.Event(),
        )
    assert rc == EXIT_RELOGIN


async def test_test_alert_title_does_not_mention_a_sensor():
    msg = dict(ALERT, sound_key="test", label="Test alert", device_name="a removed sensor")
    server = FakeServer(([msg], HANG))
    notifier = FakeNotifier()
    stop, task = _start(server, notifier, FakeAlarm())
    await _until(lambda: notifier.popups)
    assert notifier.popups[0][0] == "Test alert — your alerts work"
    stop.set()
    await task


# --- fix round 1 ------------------------------------------------------------------------


def _ack(event_id, by_member_id=9, by_email="bob@x.org"):
    return {
        "kind": "alert_ack",
        "event_id": event_id,
        "by_member_id": by_member_id,
        "by_email": by_email,
        "at": AT,
    }


def _alert(alert_id, event_id, alarm=True, **kw):
    return dict(ALERT, alert_id=alert_id, event_id=event_id, alarm=alarm, **kw)


def _pending():
    return asyncio.get_running_loop().create_future()


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


# A message sent after the one under test: once its popup shows, everything before it was
# handled, so negative assertions afterwards can actually fail.
MARK = {"kind": "device_offline", "device_id": 99, "name": "marker"}
MARK_TITLE = "Sensor offline: marker"


async def _until_marked(notifier):
    await _until(lambda: any(p[0] == MARK_TITLE for p in notifier.popups))
    await _settle()


def _real(notifier):
    return [p for p in notifier.popups if p[0] != MARK_TITLE]


async def test_alarm_failure_still_shows_popup_and_keeps_the_connection(caplog):
    gate = asyncio.Event()
    offline = {"kind": "device_offline", "device_id": 1, "name": "Hall"}
    server = FakeServer(([ALERT, gate, offline], HANG))
    notifier = FakeNotifier({TITLE: _pending()})
    alarm = FakeAlarm(fail=RuntimeError(f"boom {TOKEN}"))
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: notifier.popups)
    assert notifier.popups[0][0] == TITLE
    gate.set()
    await _until(lambda: len(notifier.popups) == 2)
    assert notifier.popups[1][0] == "Sensor offline: Hall"
    assert len(server.urls) == 1  # no reconnect
    stop.set()
    assert await task == 0
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


async def test_second_alert_handled_while_first_popup_pending():
    second = _alert(8, 4, label="Glass breaking", device_name="Hall")
    server = FakeServer(([ALERT, second], HANG))
    notifier = FakeNotifier({TITLE: _pending()})
    stop, task = _start(server, notifier, FakeAlarm())
    await _until(lambda: len(notifier.popups) == 2)
    assert notifier.popups[1][0] == "Glass breaking — Hall"
    stop.set()
    await task


async def test_ack_for_an_alert_i_never_got_is_ignored():
    gate = asyncio.Event()
    server = FakeServer(([ALERT, gate, _ack(99), MARK], HANG))
    notifier, alarm = FakeNotifier({TITLE: _pending()}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.playing)
    gate.set()
    await _until_marked(notifier)
    assert alarm.playing and alarm.stops == 0
    assert len(_real(notifier)) == 1
    stop.set()
    await task


async def test_test_alert_ack_across_household_is_ignored():
    gate = asyncio.Event()
    mine = _alert(7, None, sound_key="test", label="Test alert")
    server = FakeServer(([mine, gate, _ack(None), MARK], HANG))
    notifier = FakeNotifier({"Test alert — your alerts work": _pending()})
    alarm = FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.playing)
    gate.set()
    await _until_marked(notifier)
    assert alarm.playing and alarm.stops == 0
    assert len(_real(notifier)) == 1
    stop.set()
    await task


async def test_alarm_stops_only_when_every_sounding_alert_is_acked():
    g1, g2 = asyncio.Event(), asyncio.Event()
    second = _alert(8, 4, label="Glass breaking", device_name="Hall")
    server = FakeServer(([ALERT, second, g1, _ack(3), g2, _ack(4)], HANG))
    notifier = FakeNotifier({TITLE: _pending(), "Glass breaking — Hall": _pending()})
    alarm = FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.plays == 2)
    g1.set()
    await _until(lambda: len(notifier.popups) == 3)
    assert alarm.playing  # event 4 still sounding
    g2.set()
    await _until(lambda: not alarm.playing)
    stop.set()
    await task


async def test_new_alarm_alert_restarts_the_30s_window():
    second = _alert(8, 4)
    server = FakeServer(([ALERT, second], HANG))
    notifier = FakeNotifier({TITLE: _pending()})
    alarm = FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.plays == 2)
    assert alarm.log == ["play", "stop", "play"]
    stop.set()
    await task


async def test_stop_alarm_button_forgets_that_alert():
    gate = asyncio.Event()
    second = _alert(8, 4, label="Glass breaking", device_name="Hall")
    answer = _pending()
    server = FakeServer(([ALERT, gate, second, _ack(4)], HANG))
    notifier = FakeNotifier({TITLE: answer, "Glass breaking — Hall": _pending()})
    alarm = FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.playing)
    answer.set_result("Stop alarm")
    await _until(lambda: not alarm.playing)
    gate.set()
    # event 3 was forgotten, so acking event 4 empties the set and stops the new alarm
    await _until(lambda: alarm.plays == 2 and not alarm.playing)
    stop.set()
    await task


async def test_own_ack_falls_back_to_case_insensitive_email_without_member_id():
    gate = asyncio.Event()
    cfg = AgentConfig("http://srv", "Me@Example.com", TOKEN)  # old config: no member_id
    own = _ack(3, by_member_id=1, by_email="me@example.COM")
    server = FakeServer(([ALERT, gate, own, MARK], HANG))
    notifier, alarm = FakeNotifier({TITLE: _pending()}), FakeAlarm()
    stop = asyncio.Event()
    task = asyncio.create_task(_run(server, notifier, alarm, stop=stop, cfg=cfg))
    await _until(lambda: alarm.playing)
    gate.set()
    await _until(lambda: not alarm.playing)
    await _until_marked(notifier)
    assert len(_real(notifier)) == 1
    stop.set()
    await task


async def test_ack_for_popup_only_alert_shows_who():
    gate = asyncio.Event()
    server = FakeServer(([_alert(7, 3, alarm=False), gate, _ack(3)], HANG))
    notifier, alarm = FakeNotifier({TITLE: _pending()}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: notifier.popups)
    gate.set()
    await _until(lambda: len(notifier.popups) == 2)
    assert notifier.popups[1][0] == "bob@x.org is handling it"
    assert alarm.plays == 0
    stop.set()
    await task


# --- final review: alarm wave -----------------------------------------------------------


async def test_ringing_is_capped_at_3_minutes_until_the_alarm_goes_quiet():
    gates = [asyncio.Event() for _ in range(4)]
    alerts = [_alert(10 + i, 20 + i) for i in range(5)]
    items = [alerts[0]]
    for g, a in zip(gates, alerts[1:], strict=True):
        items += [g, a]
    server = FakeServer((items, HANG))
    notifier, alarm = FakeNotifier({TITLE: _pending()}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.plays == 1)  # t=0: ringing starts
    server.now = 100.0
    gates[0].set()
    await _until(lambda: alarm.plays == 2)  # within 3 min: a new alert restarts it
    server.now = 181.0
    gates[1].set()
    await _until(lambda: len(notifier.popups) == 3)
    await _settle()
    assert alarm.plays == 2 and alarm.stops == 1  # capped: popup only, no restart
    assert alarm.playing  # still sounding out its current 30 s
    alarm.playing = False  # the 30 s ran out: the alarm has been quiet
    server.now = 200.0
    gates[2].set()
    await _until(lambda: alarm.plays == 3)  # quiet reset the cap: rings again
    server.now = 250.0
    gates[3].set()
    await _until(lambda: alarm.plays == 4)  # 50 s into the new wave: restart allowed
    stop.set()
    await task


async def test_alert_evicted_from_known_stops_its_alarm():
    gate = asyncio.Event()
    quiet = [_alert(1000 + i, 1000 + i, alarm=False) for i in range(200)]
    server = FakeServer(([ALERT, *quiet[:199], gate, quiet[199]], HANG))
    notifier, alarm = FakeNotifier({TITLE: _pending()}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: len(notifier.popups) == 200)
    await _settle()
    assert alarm.playing  # event 3 is still remembered (200 known)
    gate.set()
    await _until(lambda: not alarm.playing)  # event 3 forgotten: nothing left to sound for
    assert alarm.plays == 1
    stop.set()
    await task


async def test_popup_only_alert_offers_only_im_on_it():
    server = FakeServer(([_alert(7, 3, alarm=False)], HANG))
    notifier, alarm = FakeNotifier(), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: notifier.popups)
    assert notifier.popups[0][2] == ["I'm on it"]
    stop.set()
    await task


# --- login expiry (M8) ------------------------------------------------------------------


def _jwt(payload):
    import base64

    def b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(payload)}.c2lnbmF0dXJl"


NOW_TS = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def test_login_expiry_decodes_exp_without_verifying():
    from ha_edge.agent import login_expiry

    exp = int(NOW_TS.timestamp()) + 3600
    assert login_expiry(_jwt({"sub": "1", "exp": exp})) == datetime.fromtimestamp(exp, UTC)
    assert login_expiry("not-a-jwt") is None
    assert login_expiry(_jwt({"sub": "1"})) is None
    assert login_expiry("a.!!!.c") is None


@pytest.mark.parametrize(
    ("hours_left", "level", "text"),
    [
        (20, logging.INFO, "stored login expires"),
        (1.5, logging.WARNING, "less than 2 h"),
        (-1, logging.WARNING, "has expired"),
    ],
)
def test_log_login_expiry(caplog, hours_left, level, text):
    from ha_edge.agent import log_login_expiry

    caplog.set_level(logging.DEBUG)
    token = _jwt({"sub": "1", "exp": int(NOW_TS.timestamp() + hours_left * 3600)})
    log_login_expiry(token, NOW_TS)
    (rec,) = caplog.records
    assert rec.levelno == level and text in rec.getMessage()
    assert token not in caplog.text and token.split(".")[1] not in caplog.text


def test_log_login_expiry_unreadable_token_says_nothing_secret(caplog):
    from ha_edge.agent import log_login_expiry

    caplog.set_level(logging.DEBUG)
    log_login_expiry(TOKEN, NOW_TS)
    assert TOKEN not in caplog.text
