"""The sensor loop: microphone -> classifier -> detector -> event queue -> server.

Three tasks run in one TaskGroup: audio, heartbeat and queue drain. Setting ``stop``
ends all of them; the microphone stream is always closed. Raw audio stays in memory;
only {type, confidence, occurred_at} for enabled sounds is ever sent. The device key
is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from ha_edge.api import (
    DeviceRevoked,
    EventQueue,
    RateLimited,
    Rejected,
    ServerUnavailable,
)
from ha_edge.audio import MicLost, Windower
from ha_edge.config import SensorConfig
from ha_edge.detector import Detector
from ha_edge.labels import build_index

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_MIC_LOST = 4
EXIT_REVOKED = 5

FIRST_HEARTBEAT_RETRY_S = 5.0
DRAIN_INTERVAL_S = 1.0
MAX_BACKOFF_S = 30.0
REVOKED_MESSAGE = "This sensor was removed. Pair it again with `ha-sensor pair`."


class _Stopped(Exception):
    """Raised by the watcher task to unwind the TaskGroup when ``stop`` is set."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_sensor(
    cfg: SensorConfig,
    *,
    client,
    classifier,
    mic,
    probe,
    stop: asyncio.Event,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Run until ``stop`` is set or a fatal condition occurs; return the exit code.

    ``monotonic`` drives detector debounce; ``now`` (timezone-aware) stamps events.
    """
    names = list(classifier.class_names)
    detector = Detector(build_index(names), num_classes=len(names))
    queue = EventQueue()
    enabled: frozenset[str] = frozenset()  # empty until the first successful heartbeat
    code = EXIT_OK

    def fatal(exit_code: int) -> None:
        nonlocal code
        if code == EXIT_OK:
            code = exit_code
        stop.set()

    async def watch_stop() -> None:
        await stop.wait()
        raise _Stopped

    async def audio_task() -> None:
        windower = Windower()
        try:
            async for chunk in mic:
                for window in windower.push(chunk):
                    scores = await asyncio.to_thread(classifier.scores, window)
                    playing = await asyncio.to_thread(probe.playing)
                    for d in detector.process(scores, monotonic(), enabled, playing):
                        log.info("detected %s (%.2f)", d.sound, d.confidence)
                        queue.put(d.sound, d.confidence, now())
        except MicLost as e:
            log.error("Microphone lost: %s. Not switching to another input; stopping.", e)
            fatal(EXIT_MIC_LOST)
            return
        if not stop.is_set():
            log.error("Microphone stream ended; stopping.")
            fatal(EXIT_MIC_LOST)

    async def heartbeat_task() -> None:
        nonlocal enabled
        interval: float | None = None  # None until the first success
        while True:
            try:
                dc = await client.heartbeat()
            except DeviceRevoked:
                log.error(REVOKED_MESSAGE)
                fatal(EXIT_REVOKED)
                return
            except (ServerUnavailable, RateLimited, Rejected) as e:
                wait = interval if interval is not None else FIRST_HEARTBEAT_RETRY_S
                if isinstance(e, RateLimited):
                    wait = max(wait, e.retry_after_s)
                log.warning("heartbeat failed (%s); retrying in %g s", e, wait)
                await sleep(wait)
                continue
            if dc.enabled_sounds != enabled or interval is None:
                log.info("enabled sounds: %s", ", ".join(sorted(dc.enabled_sounds)) or "none")
            enabled = dc.enabled_sounds
            interval = float(dc.heartbeat_interval_s)
            await sleep(dc.heartbeat_interval_s)

    async def drain_task() -> None:
        backoff = 1.0
        while True:
            try:
                sent = await queue.drain(client, now())
            except DeviceRevoked:
                log.error(REVOKED_MESSAGE)
                fatal(EXIT_REVOKED)
                return
            except ServerUnavailable as e:
                log.warning("server unavailable (%s); %d event(s) queued", e, len(queue))
                await sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue
            except RateLimited as e:
                await sleep(e.retry_after_s)
                continue
            backoff = 1.0
            if sent:
                log.info("sent %d event(s)", sent)
            await sleep(DRAIN_INTERVAL_S)

    try:
        async with mic, asyncio.TaskGroup() as tg:
            tg.create_task(watch_stop())
            tg.create_task(audio_task())
            tg.create_task(heartbeat_task())
            tg.create_task(drain_task())
    except* _Stopped:
        pass
    except* MicLost as eg:  # raised while opening the stream
        log.error("Microphone lost: %s. Stopping.", eg.exceptions[0])
        code = code or EXIT_MIC_LOST
    return code
