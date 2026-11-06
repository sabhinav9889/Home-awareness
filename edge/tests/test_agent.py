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
CFG = AgentConfig(base_url="http://srv", email="me@example.com", access_token=TOKEN)
AT = "2026-11-07T12:34:00+00:00"
HHMM = datetime(2026, 11, 7, 12, 34, tzinfo=UTC).astimezone().strftime("%H:%M")
ALERT = {
    "kind": "alert",
    "alert_id": 7,
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
    def __init__(self):
        self.plays = 0
        self.stops = 0
        self.playing = False

    async def play(self):
        self.plays += 1
        self.playing = True

    def stop(self):
        self.stops += 1
        self.playing = False


async def no_sleep(s):
    await asyncio.sleep(0)


async def _until(cond):
    async with asyncio.timeout(2):
        while not cond():
            await asyncio.sleep(0)


async def _run(server, notifier, alarm, *, stop=None, sleep=no_sleep, rand=lambda: 0.5, http=None):
    own = http is None
    http = http or httpx.AsyncClient()
    try:
        async with asyncio.timeout(5):
            return await run_agent(
                CFG,
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
    ack = {"kind": "alert_ack", "event_id": 3, "by_member_id": 1, "by_email": CFG.email, "at": AT}
    server = FakeServer(([ALERT, gate, ack], HANG))
    notifier, alarm = FakeNotifier({TITLE: asyncio.get_running_loop().create_future()}), FakeAlarm()
    stop, task = _start(server, notifier, alarm)
    await _until(lambda: alarm.playing)
    gate.set()
    await _until(lambda: not alarm.playing)
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(notifier.popups) == 1
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
