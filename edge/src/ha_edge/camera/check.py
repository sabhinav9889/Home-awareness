"""``ha-camera check``: what does the camera see right now?

Prints one line per analysed frame (torso angle or "no body", person score, whether the
household is guarding, and what would be sent) and ends with a summary and plain advice.
Nothing is sent, recorded or saved: frames stay in memory and only numbers are printed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ha_edge.api import DeviceConfig
from ha_edge.camera.capture import CameraBusy, FrameSource
from ha_edge.camera.detectors import ObjectModel, PoseModel
from ha_edge.camera.runner import DEFAULT_OBJECT_THRESHOLD, Analyzer

DARK_LEVEL = 40.0  # mean pixel value (0-255) under which the picture is too dark
MIN_BODY_SHARE = 0.2  # fraction of frames with a body below which we say so
NO_FRAME_S = 3.0

# Without a server to ask: every event on, watching on, default thresholds.
FALLBACK_CONFIG = DeviceConfig(
    frozenset({"fall", "lying_still", "person_away", "weapon_seen"}), 60, {}, True, True
)


@dataclass
class CheckSummary:
    frames: int = 0
    body_frames: int = 0
    person_frames: int = 0
    brightness: float = 0.0  # mean over frames
    would_send: dict[str, int] | None = None
    advice: str = ""


def advice(s: CheckSummary) -> str:
    if s.frames == 0:
        return (
            "No pictures came from the camera: it is probably busy or unplugged. Close any "
            "app using it (browser tab, video call) and try again."
        )
    if s.brightness < DARK_LEVEL:
        return (
            "The picture is too dark. Turn on a light or face the camera towards a "
            "brighter spot, then check again."
        )
    if s.body_frames / s.frames < MIN_BODY_SHARE:
        return (
            "No body was visible for most of the check. Stand where the camera sees your "
            "head to your hips (fall and lying-still need the shoulders and hips in view), "
            "or move the camera back."
        )
    return "The camera sees you well enough."


def _line(i: int, res, cfg: DeviceConfig) -> str:
    angle = "no body" if res.angle_deg is None else f"torso {res.angle_deg:.0f} deg"
    person = f"person {res.person_score:.2f}" if res.person_score is not None else "person -"
    guarding = "guarding yes" if cfg.guarding else "guarding no"
    send = ", ".join(s for s, _ in res.would_send) or "-"
    return f"#{i:<4} {angle:<14} {person:<12} {guarding:<12} would send: {send}"


def run_check(
    source: FrameSource,
    pose: PoseModel,
    objects: ObjectModel,
    *,
    cfg: DeviceConfig | None = None,
    seconds: float = 15.0,
    fps: float = 4.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] | None = None,
    out: Callable[[str], None] = print,
) -> CheckSummary:
    """Analyse frames for ``seconds``; nothing is sent (this function has no client)."""
    cfg = cfg if cfg is not None else FALLBACK_CONFIG
    sleep = sleep or time.sleep
    analyzer = Analyzer(pose, objects)
    s = CheckSummary(would_send={})
    brightness = 0.0
    start = clock()
    next_at = start
    none_since: float | None = None
    while clock() - start < seconds:
        try:
            frame = source.read()
        except CameraBusy:
            break
        t = clock()
        if frame is None:
            none_since = t if none_since is None else none_since
            if t - none_since > NO_FRAME_S:
                break
            sleep(1.0 / fps)
            continue
        none_since = None
        res = analyzer.analyze(frame, cfg, t, force_models=True)
        s.frames += 1
        brightness += float(np.mean(frame.image))
        s.body_frames += res.angle_deg is not None
        s.person_frames += bool(
            res.person_score
            and res.person_score >= cfg.thresholds.get("person_away", DEFAULT_OBJECT_THRESHOLD)
        )
        for sound, _ in res.would_send:
            s.would_send[sound] = s.would_send.get(sound, 0) + 1
        out(_line(s.frames, res, cfg))
        next_at += 1.0 / fps
        sleep(max(0.0, next_at - clock()))
    s.brightness = brightness / s.frames if s.frames else 0.0
    s.advice = advice(s)
    sent = ", ".join(f"{k} x{v}" for k, v in sorted(s.would_send.items())) or "nothing"
    out(
        f"Summary: {s.frames} frames, body seen in {s.body_frames}, person seen in "
        f"{s.person_frames}, brightness {s.brightness:.0f}/255. "
        f"Frames where it would send: {sent}. "
        "Nothing was sent."
    )
    out(s.advice)
    return s
