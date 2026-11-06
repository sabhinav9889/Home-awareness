"""Desktop popups and the headphone alarm for ``ha-agent``.

The alarm plays through ``pw-play`` on PipeWire's default sink, which is the headphones
while they are in use: the whole point is to cut through whatever the user is listening to.
The tone is synthesized in memory and piped to ``pw-play``'s stdin; nothing touches disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import numpy as np
from desktop_notifier import Button, Urgency
from desktop_notifier import Capability as _Capability

log = logging.getLogger(__name__)

APP_NAME = "Home Awareness"
ALARM_RATE = 48_000  # PipeWire's usual graph rate: no resampling
ALARM_AMPLITUDE = 0.5
ALARM_TONES_HZ = (880.0, 660.0)
ALARM_TONE_S = 0.25
MAX_ALARM_S = 30.0
POPUP_WAIT_S = 600.0  # stop waiting for a button after 10 minutes
_TERM_GRACE_S = 2.0


# --- popups --------------------------------------------------------------------------


class Notifier(Protocol):
    async def popup(self, title: str, body: str, actions: list[str]) -> str | None:
        """Show a popup; return the chosen action, or None (no actions, dismissed, timed out)."""
        ...


def _bold(text: str, stream: Any) -> str:
    isatty = getattr(stream, "isatty", lambda: False)
    return f"\033[1m{text}\033[0m" if isatty() else text


class DesktopNotifier:
    """Popups via the ``desktop-notifier`` library (freedesktop notifications over D-Bus).

    Without a session D-Bus or notification server, every popup is printed as a bold line
    on stderr instead (the failure is logged once). Never raises.
    """

    def __init__(
        self,
        lib: Any = None,
        lib_factory: Callable[[], Any] | None = None,
        out: Any = None,
        wait_s: float = POPUP_WAIT_S,
    ) -> None:
        self._lib = lib
        self._factory = lib_factory or self._default_factory
        self._out = out
        self._wait_s = wait_s
        self._broken = False
        self._hinted = False

    @staticmethod
    def _default_factory() -> Any:
        from desktop_notifier import DesktopNotifier as _Lib

        return _Lib(app_name=APP_NAME)

    @property
    def _stream(self) -> Any:
        return self._out or sys.stderr

    def _print(self, title: str, body: str) -> None:
        print(_bold(f"{title} — {body}", self._stream), file=self._stream, flush=True)

    def _hint(self, actions: Sequence[str]) -> None:
        if actions and not self._hinted:
            self._hinted = True
            print(
                "(This desktop can't show buttons: the alarm stops by itself after 30 s, "
                "or press Ctrl-C here to stop it and exit.)",
                file=self._stream,
                flush=True,
            )

    def _fallback(self, title: str, body: str, actions: Sequence[str], err: Exception) -> None:
        if not self._broken:
            self._broken = True
            log.warning(
                "desktop notifications unavailable (%s); showing alerts in this terminal",
                type(err).__name__,
            )
        self._print(title, body)
        self._hint(actions)

    async def popup(self, title: str, body: str, actions: list[str]) -> str | None:
        try:
            if self._lib is None:
                self._lib = self._factory()
            caps = await self._lib.get_capabilities()
        except Exception as e:  # noqa: BLE001  no D-Bus / no notification server
            self._fallback(title, body, actions, e)
            return None
        use_buttons = bool(actions) and _Capability.BUTTONS in caps
        loop = asyncio.get_running_loop()
        chosen: asyncio.Future[str | None] = loop.create_future()

        def choose(value: str | None) -> None:
            if not chosen.done():
                chosen.set_result(value)

        buttons = (
            [Button(a, on_pressed=lambda a=a: choose(a)) for a in actions] if use_buttons else []
        )
        try:
            await self._lib.send(
                title,
                body,
                urgency=Urgency.Critical if actions else Urgency.Normal,
                buttons=buttons,
                on_clicked=lambda: choose(None),
                on_dismissed=lambda: choose(None),
            )
        except Exception as e:  # noqa: BLE001
            self._fallback(title, body, actions, e)
            return None
        if not use_buttons:
            self._hint(actions)
            return None
        try:
            async with asyncio.timeout(self._wait_s):
                return await chosen
        except TimeoutError:
            return None


# --- alarm ---------------------------------------------------------------------------


def synth_alarm(seconds: float, rate: int = ALARM_RATE) -> bytes:
    """Alternating 880/660 Hz tones (0.25 s each), amplitude 0.5, as s16le mono PCM."""
    n = round(seconds * rate)
    t = np.arange(n, dtype=np.float64) / rate
    which = (np.arange(n) // int(ALARM_TONE_S * rate)) % 2
    freq = np.where(which == 0, ALARM_TONES_HZ[0], ALARM_TONES_HZ[1])
    wave = ALARM_AMPLITUDE * np.sin(2 * np.pi * freq * t)
    return (wave * 32767).astype("<i2").tobytes()


Spawn = Callable[..., Awaitable[Any]]


async def _spawn_pw_play(*argv: str) -> Any:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class AlarmPlayer:
    """Plays the alarm on the default output for at most ``max_s`` seconds.

    ``play()`` starts it and returns; ``stop()`` silences it at once; ``wait()`` waits for
    it to end. A second ``play()`` while one is sounding does nothing. A ``stop()`` always
    wins over a ``play()`` that started before it, even one still waiting for the previous
    alarm to die or for ``pw-play`` to start.
    """

    def __init__(
        self,
        spawn: Spawn = _spawn_pw_play,
        max_s: float = MAX_ALARM_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._spawn = spawn
        self._max_s = max_s
        self._sleep = sleep
        self._proc: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False  # stop() called; the task is still reaping the process
        self._lock = asyncio.Lock()  # one play() at a time: never two alarms at once
        self._gen = 0  # bumped by stop(): a play() begun before a stop() must not sound
        self.unavailable = False  # pw-play could not be started

    @property
    def playing(self) -> bool:
        return self._running and not self._stopping

    @property
    def _running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _argv(self) -> tuple[str, ...]:
        return (
            "pw-play",
            "--rate",
            str(ALARM_RATE),
            "--channels",
            "1",
            "--format",
            "s16",
            "--media-role",
            "Notification",
            "-",
        )

    async def play(self) -> None:
        gen = self._gen
        async with self._lock:
            await self._play(gen)

    async def _play(self, gen: int) -> None:
        if gen != self._gen:
            return  # stopped while waiting for the lock
        if self.playing:
            return
        if self._running:
            await self.wait()  # a stopped alarm finishes dying first
            if gen != self._gen:
                return  # stopped while the old alarm was dying
        self._stopping = False
        try:
            proc = await self._spawn(*self._argv())
        except OSError as e:
            if not self.unavailable:
                log.error("cannot play the alarm: pw-play failed to start (%s)", e)
            self.unavailable = True
            return
        self.unavailable = False
        self._proc = proc
        self._task = asyncio.create_task(self._run(proc))
        if gen != self._gen:
            self.stop()  # stopped while pw-play was starting: silence it; _run reaps it

    async def _feed(self, proc: Any) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.write(synth_alarm(self._max_s))
            await proc.stdin.drain()
            proc.stdin.close()

    async def _run(self, proc: Any) -> None:
        feed = asyncio.create_task(self._feed(proc))
        exited = asyncio.create_task(proc.wait())
        limit = asyncio.create_task(self._sleep(self._max_s))
        try:
            await asyncio.wait({exited, limit}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (feed, limit):
                t.cancel()
            self._terminate(proc)
            try:
                async with asyncio.timeout(_TERM_GRACE_S):
                    await exited
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            except asyncio.CancelledError:
                exited.cancel()
                raise
            await asyncio.gather(feed, limit, return_exceptions=True)
            if self._proc is proc:
                self._proc = None

    @staticmethod
    def _terminate(proc: Any) -> None:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    def stop(self) -> None:
        self._gen += 1
        if self._running:
            self._stopping = True
        if self._proc is not None:
            self._terminate(self._proc)  # silence now; _run reaps the process

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def aclose(self) -> None:
        self.stop()
        await self.wait()
