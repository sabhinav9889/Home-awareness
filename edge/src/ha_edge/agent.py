"""The alert agent: listens on the server's live socket and turns alerts into popups + alarm.

The access token travels only in the socket's query string (as the server requires) and in
the ack request's Authorization header. It is never logged: logs name the base URL only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ha_edge.config import AgentConfig
from ha_edge.notify import Notifier

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RELOGIN = 6

AUTH_CLOSE_CODES = frozenset({4401, 4403})
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
BACKOFF_JITTER = 0.2
STABLE_AFTER_S = 60.0  # a connection up this long resets the backoff

IM_ON_IT = "I'm on it"
STOP_ALARM = "Stop alarm"
RELOGIN_TEXT = "Please log in again: ha-agent login"
FOOTER = "Home Awareness"
TEST_SOUND_KEY = "test"  # POST /me/test-alert
MAX_KNOWN_ALERTS = 200
MAX_RING_S = 180.0  # continuous ringing (restarts included) is capped at 3 minutes


class ConnectionClosed(Exception):
    def __init__(self, code: int | None) -> None:
        super().__init__(f"closed with code {code}")
        self.code = code


class Alarm(Protocol):
    @property
    def playing(self) -> bool: ...

    async def play(self) -> None: ...

    def stop(self) -> None: ...


Connect = Callable[[str], AbstractAsyncContextManager[AsyncIterator[str]]]


def _unwrap(group: BaseExceptionGroup) -> BaseException:
    """The error that matters inside a TaskGroup's exception group (ConnectionClosed first)."""
    closed = group.subgroup(ConnectionClosed)
    leaves = closed.exceptions if closed is not None else group.exceptions
    first = leaves[0]
    return _unwrap(first) if isinstance(first, BaseExceptionGroup) else first


def ws_connector(http: httpx.AsyncClient | None, aconnect: Any = None) -> Connect:
    """``connect(url)`` over httpx-ws; a close or network error raises ConnectionClosed."""
    from httpx_ws import WebSocketDisconnect, WebSocketNetworkError, aconnect_ws

    aconnect = aconnect or aconnect_ws

    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[AsyncIterator[str]]:
        try:
            async with aconnect(url, http) as ws:

                async def messages() -> AsyncIterator[str]:
                    while True:
                        try:
                            yield await ws.receive_text()
                        except WebSocketDisconnect as e:
                            raise ConnectionClosed(e.code) from None
                        except WebSocketNetworkError:
                            raise ConnectionClosed(None) from None

                yield messages()
        except BaseExceptionGroup as eg:
            # httpx-ws runs the session in a TaskGroup, so the close arrives wrapped.
            raise _unwrap(eg) from None

    return connect


def _hhmm(at: Any) -> str:
    try:
        return datetime.fromisoformat(str(at)).astimezone().strftime("%H:%M")
    except ValueError:
        return datetime.now().strftime("%H:%M")


class _Agent:
    def __init__(
        self,
        cfg: AgentConfig,
        notifier: Notifier,
        alarm: Alarm,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.base = cfg.base_url.rstrip("/")
        self.notifier = notifier
        self.alarm = alarm
        self.http = http
        self.tasks: set[asyncio.Task[Any]] = set()
        # Alerts are keyed by event (what alert_ack names), or by alert id for test alerts.
        self.known: dict[tuple[str, int], None] = {}  # alerts I received (bounded, oldest first)
        self.sounding: set[tuple[str, int]] = set()  # alerts the alarm is sounding for
        self.ring_started: float | None = None  # when the current ringing wave began

    @staticmethod
    def _key(msg: dict[str, Any]) -> tuple[str, int] | None:
        if msg.get("event_id") is not None:
            return ("event", msg["event_id"])
        if msg.get("alert_id") is not None:
            return ("alert", msg["alert_id"])
        return None

    def _is_me(self, msg: dict[str, Any]) -> bool:
        if self.cfg.member_id is not None:
            return msg.get("by_member_id") == self.cfg.member_id
        by = msg.get("by_email")
        return isinstance(by, str) and by.casefold() == self.cfg.email.casefold()

    def _redact(self, text: str) -> str:
        return text.replace(self.cfg.access_token, "***") if self.cfg.access_token else text

    def spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            err = task.exception()
            log.error("alert handling failed: %s: %s", type(err).__name__, self._redact(str(err)))

    async def popup(self, title: str, body: str = FOOTER, actions: list[str] | None = None) -> Any:
        return await self.notifier.popup(title, body, actions or [])

    async def handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            kind = msg.get("kind")
        except (ValueError, AttributeError):
            log.warning("ignoring a malformed message from the server")
            return
        if kind == "alert":
            await self.on_alert(msg)
        elif kind == "alert_ack":
            self.on_ack(msg)
        elif kind == "device_offline":
            name = msg.get("name") or "a sensor"
            log.info("sensor offline: %s", name)
            self.spawn(self.popup(f"Sensor offline: {name}"))
        elif kind == "device_online":
            log.info("sensor online: %s", msg.get("name"))

    async def on_alert(self, msg: dict[str, Any]) -> None:
        label = msg.get("label") or "Sound"
        device = msg.get("device_name") or "a sensor"
        log.info("alert %s: %s on %s", msg.get("alert_id"), label, device)
        title = f"{label} — {device}"
        if msg.get("sound_key") == TEST_SOUND_KEY:  # no sensor involved
            title = "Test alert — your alerts work"
        key = self._key(msg)
        if key is not None:
            self.known[key] = None
        # Popup first, so a failing alarm can never cost the alert. It is shown even when
        # only the alarm was asked for: it is the stop button.
        actions = [IM_ON_IT, STOP_ALARM] if msg.get("alarm") else [IM_ON_IT]
        self.spawn(
            self._alert_popup(key, msg.get("alert_id"), title, _hhmm(msg.get("at")), actions)
        )
        try:
            if msg.get("alarm"):
                await self._ring(key)
        finally:
            self._forget_oldest()

    async def _ring(self, key: tuple[str, int] | None) -> None:
        now = self.clock()
        if not self.alarm.playing:
            self.sounding.clear()  # anything left over ended on its own (30 s limit)
            self.ring_started = None  # the alarm has been quiet: a new wave may ring
        if key is not None:
            self.sounding.add(key)
        if self.ring_started is None:
            self.ring_started = now
        if self.alarm.playing:
            if now - self.ring_started >= MAX_RING_S:
                return  # rung for 3 minutes already: the popup alone until it goes quiet
            self.alarm.stop()  # restart: a new alert gets a full 30 s
        await self.alarm.play()

    def _forget_oldest(self) -> None:
        """Keep ``known`` bounded; an alert forgotten here can no longer keep the alarm on."""
        while len(self.known) > MAX_KNOWN_ALERTS:
            old = next(iter(self.known))
            del self.known[old]
            if old in self.sounding:
                self.sounding.discard(old)
                if not self.sounding:
                    self.alarm.stop()

    async def _alert_popup(
        self,
        key: tuple[str, int] | None,
        alert_id: Any,
        title: str,
        hhmm: str,
        actions: list[str],
    ) -> None:
        action = await self.popup(title, f"{hhmm} · {FOOTER}", actions)
        if action in (STOP_ALARM, IM_ON_IT):
            # A button press is the user, at this laptop: silence it outright.
            self.sounding.clear()
            self.alarm.stop()
        if action == IM_ON_IT:
            await self.ack(alert_id)

    async def ack(self, alert_id: Any) -> None:
        url = f"{self.base}/alerts/{alert_id}/ack"
        headers = {"Authorization": f"Bearer {self.cfg.access_token}"}
        try:
            r = await self.http.post(url, headers=headers, timeout=10)
        except httpx.HTTPError as e:
            log.warning("ack of alert %s failed: %s", alert_id, type(e).__name__)
            await self.popup("Could not send “I'm on it” — check the dashboard")
            return
        if r.status_code == 200:
            log.info("acknowledged alert %s", alert_id)
        elif r.status_code == 409:
            by = _json(r).get("by") or "Someone else"
            await self.popup(f"{by} is already handling it")
        elif r.status_code == 401:
            await self.popup(RELOGIN_TEXT)
        else:
            log.warning("ack of alert %s rejected: HTTP %s", alert_id, r.status_code)
            await self.popup("Could not send “I'm on it” — check the dashboard")

    def on_ack(self, msg: dict[str, Any]) -> None:
        event_id = msg.get("event_id")
        key = ("event", event_id)
        if event_id is None or key not in self.known:
            return  # someone else's alert (or their test alert): not ours to silence
        del self.known[key]
        if key in self.sounding:
            self.sounding.discard(key)
            if not self.sounding:
                self.alarm.stop()
        by = msg.get("by_email")
        if by and not self._is_me(msg):
            log.info("%s is handling it", by)
            self.spawn(self.popup(f"{by} is handling it"))

    async def close(self) -> None:
        self.alarm.stop()
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def run_agent(
    cfg: AgentConfig,
    *,
    connect: Connect,
    notifier: Notifier,
    alarm: Alarm,
    clock: Callable[[], float] = time.monotonic,
    http: httpx.AsyncClient,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    stop: asyncio.Event | None = None,
) -> int:
    """Run until ``stop`` is set (returns 0) or the server rejects the login (returns 6)."""
    stop = stop or asyncio.Event()
    agent = _Agent(cfg, notifier, alarm, http, clock)
    loop_task = asyncio.create_task(_connection_loop(agent, connect, clock, sleep, rand))
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({loop_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if loop_task.done():
            return loop_task.result()
        return EXIT_OK
    finally:
        for t in (loop_task, stop_task):
            t.cancel()
        await asyncio.gather(loop_task, stop_task, return_exceptions=True)
        await agent.close()


async def _connection_loop(
    agent: _Agent,
    connect: Connect,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[Any]],
    rand: Callable[[], float],
) -> int:
    url = f"{agent.base}/ws?token={quote(agent.cfg.access_token, safe='')}"
    failures = 0
    while True:
        connected_at: float | None = None
        code: int | None = None
        try:
            async with connect(url) as messages:
                connected_at = clock()
                log.info("connected to %s", agent.base)
                async for raw in messages:
                    try:
                        await agent.handle(raw)
                    except Exception as e:  # noqa: BLE001  one bad message never costs the link
                        log.error("handling a message failed: %s", type(e).__name__)
        except ConnectionClosed as e:
            code = e.code
        except (OSError, httpx.HTTPError) as e:
            log.warning("cannot reach %s: %s", agent.base, type(e).__name__)
        except Exception as e:  # noqa: BLE001  e.g. httpx-ws upgrade errors: keep retrying
            log.warning(
                "connection to %s failed: %s: %s",
                agent.base,
                type(e).__name__,
                agent._redact(str(e)),
            )
        if code in AUTH_CLOSE_CODES:
            log.error("the server rejected this login (close code %s)", code)
            agent.alarm.stop()
            with contextlib.suppress(Exception):
                await agent.popup(RELOGIN_TEXT)
            return EXIT_RELOGIN
        if connected_at is not None:
            if clock() - connected_at >= STABLE_AFTER_S:
                failures = 0
            log.warning("disconnected from %s (code %s)", agent.base, code)
        delay = min(BACKOFF_MAX_S, BACKOFF_START_S * 2**failures)
        delay *= 1 + BACKOFF_JITTER * (2 * rand() - 1)
        failures = min(failures + 1, 16)  # 2**16 s is far past the cap
        log.info("reconnecting in %.1f s", delay)
        await sleep(delay)
