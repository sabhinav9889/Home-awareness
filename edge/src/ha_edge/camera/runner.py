"""The camera loop: frames -> pose / object models -> rules -> event queue -> server.

Five tasks run in one TaskGroup (watch-stop, frames, heartbeat, config refresh, queue
drain). Frames and snapshots live in memory only; nothing here writes to disk. Only
{type, confidence, occurred_at} plus, if the household allows it, one still JPEG of the
frame that triggered the event ever leaves this computer. The device key is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ha_edge.api import (
    ApiError,
    DeviceConfig,
    DeviceRevoked,
    EventQueue,
    RateLimited,
    ServerUnavailable,
)
from ha_edge.camera.capture import CameraBusy, Frame, FrameSource
from ha_edge.camera.detectors import PERSON_LABEL, ObjectModel, PoseModel, weapon_labels
from ha_edge.camera.pose_rules import (
    FallDetector,
    LyingStillDetector,
    PersonTracker,
    torso_sample,
)
from ha_edge.camera.snapshot import encode_snapshot
from ha_edge.sensor import EXIT_OK, EXIT_REVOKED, REVOKED_MESSAGE

log = logging.getLogger(__name__)

COOLDOWNS_S: dict[str, float] = {
    "fall": 120.0,
    "lying_still": 600.0,
    "person_away": 300.0,
    "weapon_seen": 300.0,
}
RULE_CONFIDENCE = 0.9  # fall / lying_still come from rules, not a model score
DEFAULT_OBJECT_THRESHOLD = 0.5
CONFIG_REFRESH_S = 15.0
NO_FRAME_S = 3.0  # no frame for this long: the camera is busy or unplugged
REOPEN_S = 5.0
FIRST_HEARTBEAT_RETRY_S = 5.0
DRAIN_INTERVAL_S = 1.0
MAX_BACKOFF_S = 30.0
BUSY_MESSAGE = (
    "camera busy or unplugged: close any app using it (browser, video call) or plug it in; "
    f"trying again every {REOPEN_S:g} s"
)


class _Stopped(Exception):
    """Raised by the watcher task to unwind the TaskGroup when ``stop`` is set."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class FrameResult:
    angle_deg: float | None = None  # None: no body found (or pose not run)
    pose_ran: bool = False
    person_score: float | None = None  # None: object model not run
    would_send: list[tuple[str, float]] = field(default_factory=list)


class Analyzer:
    """Both models plus the rules, for one frame at a time (blocking; run in a thread).

    Returns the events the current config allows, before cooldowns. ``force_models``
    runs both models whatever the config says (``ha-camera check`` shows what they see).
    """

    def __init__(self, pose: PoseModel, objects: ObjectModel) -> None:
        self.pose = pose
        self.objects = objects
        self.fall = FallDetector()
        self.lying = LyingStillDetector()
        self.tracker = PersonTracker()

    def analyze(
        self, frame: Frame, cfg: DeviceConfig, t: float, *, force_models: bool = False
    ) -> FrameResult:
        enabled = cfg.enabled_sounds
        res = FrameResult()
        run_pose = force_models or bool(enabled & {"fall", "lying_still"})
        want_person = "person_away" in enabled and cfg.guarding
        want_weapon = "weapon_seen" in enabled
        run_objects = force_models or want_person or want_weapon

        if run_pose:
            res.pose_ran = True
            lm = self.pose.torso(frame.image)
            sample = torso_sample(lm, t) if lm is not None else None
            res.angle_deg = sample.angle_deg if sample is not None else None
            fell = self.fall.update(sample)
            lying = self.lying.update(sample, t=t)  # always pass t, also with no sample
            if fell and "fall" in enabled:
                res.would_send.append(("fall", RULE_CONFIDENCE))
            if lying and "lying_still" in enabled:
                res.would_send.append(("lying_still", RULE_CONFIDENCE))

        if run_objects:
            needed = set()
            if force_models or want_person:
                needed.add(PERSON_LABEL)
            if force_models or want_weapon:
                needed |= weapon_labels
            best: dict[str, float] = {}
            for d in self.objects.detect(frame.image):
                if d.label in needed and d.score > best.get(d.label, 0.0):
                    best[d.label] = d.score
            person = best.get(PERSON_LABEL, 0.0)
            res.person_score = person
            if want_person or force_models:
                hit = person >= cfg.thresholds.get("person_away", DEFAULT_OBJECT_THRESHOLD)
                present = self.tracker.update(hit)
                if present and want_person:
                    res.would_send.append(("person_away", person))
            else:
                self.tracker = PersonTracker()  # not watching for people: forget old hits
            if want_weapon:
                _label, score = max(
                    ((lab, best.get(lab, 0.0)) for lab in weapon_labels), key=lambda x: x[1]
                )
                if score >= cfg.thresholds.get("weapon_seen", DEFAULT_OBJECT_THRESHOLD):
                    res.would_send.append(("weapon_seen", score))
        else:
            self.tracker = PersonTracker()
        return res


def _close(obj: Any, what: str) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        log.warning("closing the %s failed", what, exc_info=True)


async def run_camera(
    client,
    source: FrameSource | Callable[[], FrameSource],
    pose: PoseModel,
    objects: ObjectModel,
    *,
    clock: Callable[[], float] = time.monotonic,
    fps: float = 4.0,
    config_refresh_s: float = CONFIG_REFRESH_S,
    stop: asyncio.Event,
    now: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Run until ``stop`` is set or the device is revoked; return the exit code.

    ``source`` is a frame source, or a factory returning one (it may raise CameraBusy);
    a factory lets a busy or unplugged camera be reopened. ``clock`` drives cooldowns and
    pacing; ``now`` (timezone-aware) stamps events: the time of the triggering frame.
    """
    factory: Callable[[], FrameSource] = source if callable(source) else (lambda: source)  # type: ignore[assignment,return-value]
    reopenable = callable(source)
    analyzer = Analyzer(pose, objects)
    queue = EventQueue(clock=clock)
    cfg: DeviceConfig | None = None  # None until the first successful heartbeat
    last_sent: dict[str, float] = {}
    current: list[FrameSource] = []  # at most one open source (closed in `finally`)
    code = EXIT_OK
    period = 1.0 / fps

    def fatal(exit_code: int) -> None:
        nonlocal code
        if code == EXIT_OK:
            code = exit_code
        stop.set()

    def set_config(dc: DeviceConfig) -> None:
        nonlocal cfg
        old = cfg
        if old is None or dc.enabled_sounds != old.enabled_sounds:
            log.info("enabled: %s", ", ".join(sorted(dc.enabled_sounds)) or "none")
        if old is not None and dc.guarding != old.guarding:
            log.info("guarding is now %s", "on" if dc.guarding else "off")
        cfg = dc
        queue.retain(dc.enabled_sounds.__contains__)

    async def watch_stop() -> None:
        await stop.wait()
        raise _Stopped

    async def open_source() -> FrameSource:
        fut = asyncio.ensure_future(asyncio.to_thread(factory))
        try:
            src = await asyncio.shield(fut)
        except asyncio.CancelledError:
            # opened while we were stopping: do not leak the camera
            fut.add_done_callback(
                lambda f: (
                    _close(f.result(), "camera")
                    if not f.cancelled() and not f.exception()
                    else None
                )
            )
            raise
        current.append(src)
        return src

    def emit(sound: str, conf: float, frame: Frame, t: float) -> None:
        assert cfg is not None
        last = last_sent.get(sound)
        if last is not None and t - last < COOLDOWNS_S[sound]:
            return
        last_sent[sound] = t
        age = max(0.0, t - frame.captured_at)
        occurred_at = now() - timedelta(seconds=age)  # the frame's time, not an earlier one
        snap = encode_snapshot(frame, t) if cfg.send_snapshots else None
        log.info("detected %s (%.2f)%s", sound, conf, " with snapshot" if snap else "")
        queue.put(sound, conf, occurred_at, snap)

    async def frame_task() -> None:
        src: FrameSource | None = None
        busy = False
        none_since: float | None = None
        warned_model = False
        next_at = clock()

        async def lose() -> None:
            nonlocal src, busy, none_since
            if not busy:
                busy = True
                log.warning(BUSY_MESSAGE)
            none_since = None
            if reopenable and src is not None:
                old, src = src, None
                if old in current:
                    current.remove(old)
                await asyncio.to_thread(_close, old, "camera")
            await sleep(REOPEN_S)

        while True:
            if src is None:
                try:
                    src = await open_source()
                except CameraBusy:
                    await lose()
                    continue
                except Exception as e:  # noqa: BLE001 - e.g. a driver error: never crash
                    log.warning("cannot open the camera (%s)", e)
                    await lose()
                    continue
            try:
                frame = await asyncio.to_thread(src.read)
            except CameraBusy:
                await lose()
                continue
            t = clock()
            if frame is None:
                if none_since is None:
                    none_since = t
                if t - none_since > NO_FRAME_S or busy:
                    await lose()
                else:
                    await sleep(period)
                continue
            none_since = None
            if busy:
                busy = False
                log.info("camera is back")
            if cfg is not None:
                try:
                    res = await asyncio.to_thread(analyzer.analyze, frame, cfg, t)
                    warned_model = False
                except Exception as e:  # noqa: BLE001 - a model hiccup must not stop heartbeats
                    if not warned_model:
                        warned_model = True
                        log.warning("analysing a frame failed (%s: %s)", type(e).__name__, e)
                else:
                    for sound, conf in res.would_send:
                        emit(sound, conf, frame, t)
            next_at += period
            delay = next_at - clock()
            if delay < 0:
                next_at = clock()
                delay = 0.0
            await sleep(delay)

    async def heartbeat_task() -> None:
        interval: int | None = None
        while True:
            try:
                dc = await client.heartbeat()
            except DeviceRevoked:
                log.error(REVOKED_MESSAGE)
                fatal(EXIT_REVOKED)
                return
            except ApiError as e:
                wait = interval if interval is not None else FIRST_HEARTBEAT_RETRY_S
                if isinstance(e, RateLimited):
                    wait = max(wait, e.retry_after_s)
                log.warning("heartbeat failed (%s); retrying in %g s", e, wait)
                await sleep(wait)
                continue
            set_config(dc)
            interval = max(1, dc.heartbeat_interval_s)
            await sleep(interval)

    async def refresh_task() -> None:
        while True:
            await sleep(config_refresh_s)
            try:
                dc = await client.config()
            except DeviceRevoked:
                log.error(REVOKED_MESSAGE)
                fatal(EXIT_REVOKED)
                return
            except ApiError as e:
                log.warning("config refresh failed (%s)", e)
                continue
            set_config(dc)

    def allowed(sound: str) -> bool:
        if cfg is None or sound not in cfg.enabled_sounds:
            return False
        return cfg.guarding if sound == "person_away" else True

    async def drain_task() -> None:
        backoff = 1.0
        while True:
            try:
                sent = await queue.drain(client, now(), allow=allowed)
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
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(watch_stop())
                tg.create_task(frame_task())
                tg.create_task(heartbeat_task())
                tg.create_task(refresh_task())
                tg.create_task(drain_task())
        except* _Stopped:
            pass
    finally:
        for s in current:
            _close(s, "camera")
        current.clear()
        _close(pose, "pose model")
        _close(objects, "object model")
    return code
