"""Pure fall / lying-still / person-presence logic on torso samples (no model, no clock)."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

# Defaults (named so they can be tuned and tested).
MIN_VISIBILITY = 0.5
FALL_WINDOW_S = 1.0
UPRIGHT_DEG = 35.0
DOWN_DEG = 60.0
MIN_DROP = 0.15
MIN_STILL_S = 120.0
MAX_MOTION = 0.03
LYING_GAP_RESET_S = 5.0
TRACK_MIN_HITS = 3
TRACK_OF = 5

Point = tuple[float, float, float]  # x, y (0-1 image coords), visibility


@dataclass
class Landmarks:
    left_shoulder: Point
    right_shoulder: Point
    left_hip: Point
    right_hip: Point


@dataclass(frozen=True)
class TorsoSample:
    t: float
    angle_deg: float  # from vertical: 0 upright, 90 horizontal
    hip_y: float  # mid-hip y; image y grows downward
    cx: float
    cy: float


def torso_sample(
    lm: Landmarks, t: float, *, min_visibility: float = MIN_VISIBILITY
) -> TorsoSample | None:
    pts = (lm.left_shoulder, lm.right_shoulder, lm.left_hip, lm.right_hip)
    if any(p[2] < min_visibility for p in pts):
        return None
    sx = (lm.left_shoulder[0] + lm.right_shoulder[0]) / 2
    sy = (lm.left_shoulder[1] + lm.right_shoulder[1]) / 2
    hx = (lm.left_hip[0] + lm.right_hip[0]) / 2
    hy = (lm.left_hip[1] + lm.right_hip[1]) / 2
    dx, dy = hx - sx, hy - sy
    if dx == 0 and dy == 0:
        return None
    angle = math.degrees(math.atan2(abs(dx), abs(dy)))
    return TorsoSample(t=t, angle_deg=angle, hip_y=hy, cx=(sx + hx) / 2, cy=(sy + hy) / 2)


class FallDetector:
    """True once when, within the window, the torso goes upright -> down and the hips drop."""

    def __init__(
        self,
        window_s: float = FALL_WINDOW_S,
        upright_deg: float = UPRIGHT_DEG,
        down_deg: float = DOWN_DEG,
        min_drop: float = MIN_DROP,
    ) -> None:
        self.window_s = window_s
        self.upright_deg = upright_deg
        self.down_deg = down_deg
        self.min_drop = min_drop
        self._win: deque[TorsoSample] = deque()
        self._armed = True

    def update(self, sample: TorsoSample | None) -> bool:
        if sample is None:
            return False
        while self._win and sample.t - self._win[0].t > self.window_s:
            self._win.popleft()
        self._win.append(sample)
        if sample.angle_deg <= self.upright_deg:
            self._armed = True
        if not self._armed or sample.angle_deg < self.down_deg:
            return False
        for old in self._win:
            if old.angle_deg <= self.upright_deg and sample.hip_y - old.hip_y >= self.min_drop:
                self._armed = False
                return True
        return False


class LyingStillDetector:
    """True once when the torso stays down and centre-still for min_still_s.

    A None sample never counts as evidence. Pass `t` with None so the gap is timed; a gap
    over 5 s (None or missing samples) resets the timer.
    """

    def __init__(
        self,
        min_still_s: float = MIN_STILL_S,
        down_deg: float = DOWN_DEG,
        max_motion: float = MAX_MOTION,
        upright_deg: float = UPRIGHT_DEG,
        gap_reset_s: float = LYING_GAP_RESET_S,
    ) -> None:
        self.min_still_s = min_still_s
        self.down_deg = down_deg
        self.max_motion = max_motion
        self.upright_deg = upright_deg
        self.gap_reset_s = gap_reset_s
        self._anchor: TorsoSample | None = None
        self._last_t: float | None = None
        self._armed = True

    def _reset(self) -> None:
        self._anchor = None

    def rearm(self) -> None:
        """Let the next still-lying frame report again (the caller held the last report back)."""
        self._armed = True

    def update(self, sample: TorsoSample | None, t: float | None = None) -> bool:
        if sample is None:
            if t is not None and self._last_t is not None and t - self._last_t > self.gap_reset_s:
                self._reset()
            return False
        if self._last_t is not None and sample.t - self._last_t > self.gap_reset_s:
            self._reset()
        self._last_t = sample.t
        if sample.angle_deg <= self.upright_deg:
            self._armed = True
        if sample.angle_deg < self.down_deg:
            self._reset()
            return False
        a = self._anchor
        if a is None or math.hypot(sample.cx - a.cx, sample.cy - a.cy) > self.max_motion:
            self._anchor = sample
            return False
        if self._armed and sample.t - a.t >= self.min_still_s:
            self._armed = False
            return True
        return False


class PersonTracker:
    """True while at least min_hits of the last `of` inference frames saw a person."""

    def __init__(self, min_hits: int = TRACK_MIN_HITS, of: int = TRACK_OF) -> None:
        self.min_hits = min_hits
        self._hist: deque[bool] = deque(maxlen=of)

    def update(self, person_seen: bool) -> bool:
        self._hist.append(bool(person_seen))
        return sum(self._hist) >= self.min_hits
