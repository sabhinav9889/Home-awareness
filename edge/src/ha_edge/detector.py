"""Turns per-window YAMNet scores into debounced, thresholded detections.

A window that crosses a sound's threshold is held for one more window (about 0.5 s) and
emitted once with the higher of the two confidences: the first crossing is often the
sound's onset, and its peak, one window later, would otherwise be debounced away (a
member with a stricter threshold would never see it). Debounce counts from the emit.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from ha_edge.labels import IndexRule, sound_confidence

MIN_THRESHOLD = 0.05
MAX_THRESHOLD = 0.95


@dataclass
class Detection:
    sound: str
    confidence: float
    at: float


class Detector:
    def __init__(
        self,
        index: Mapping[str, Sequence[int | IndexRule]],
        threshold: float = 0.30,
        boost: float = 0.20,
        debounce_s: float = 2.0,
        num_classes: int | None = None,
    ) -> None:
        # Plain class indices (as in older callers and tests) are unconditional rules.
        self._index: dict[str, tuple[IndexRule, ...]] = {
            sound: tuple(r if isinstance(r, IndexRule) else IndexRule(r) for r in rules)
            for sound, rules in index.items()
        }
        self._threshold = threshold
        self._boost = boost
        self._debounce_s = debounce_s
        self._num_classes = num_classes
        self._needed = (
            max(
                (
                    i
                    for rules in self._index.values()
                    for r in rules
                    for i in (r.index, *r.unless_stronger)
                ),
                default=-1,
            )
            + 1
        )
        self._last_emit: dict[str, float] = {}
        self._held: dict[str, Detection] = {}

    def _base(self, sound: str, thresholds: Mapping[str, float] | None) -> float:
        """The sound's threshold from the server, else the global default. Received values
        are clamped into [MIN_THRESHOLD, MAX_THRESHOLD]; non-finite ones are ignored."""
        t = thresholds.get(sound) if thresholds else None
        if t is None or not math.isfinite(t):
            return self._threshold
        return min(max(t, MIN_THRESHOLD), MAX_THRESHOLD)

    def playback_matters(
        self,
        scores: np.ndarray,
        enabled: frozenset[str],
        thresholds: Mapping[str, float] | None = None,
    ) -> bool:
        """True if some enabled sound scores in [threshold, boosted threshold).

        Only then can "is the machine playing audio?" change the outcome, so the caller
        can skip the playback probe for every other window. Uses the same per-sound
        thresholds as ``process``.
        """
        scores = np.asarray(scores)
        for sound, rules in self._index.items():
            if sound not in enabled:
                continue
            base = self._base(sound, thresholds)
            boosted = min(base + self._boost, MAX_THRESHOLD)
            if base <= sound_confidence(scores, rules) < boosted:
                return True
        return False

    def process(
        self,
        scores: np.ndarray,
        at: float,
        enabled: frozenset[str],
        playing: bool,
        thresholds: Mapping[str, float] | None = None,
    ) -> list[Detection]:
        scores = np.asarray(scores)
        expected = self._num_classes
        if scores.ndim != 1 or (expected is not None and scores.shape[0] != expected):
            raise ValueError(f"scores must be 1-D of length {expected}, got {scores.shape}")
        if scores.shape[0] < self._needed:
            raise ValueError(f"scores too short: {scores.shape[0]} < {self._needed}")
        boost = self._boost if playing else 0.0
        out: list[Detection] = []
        for sound, rules in self._index.items():
            if sound not in enabled:
                continue
            limit = min(self._base(sound, thresholds) + boost, MAX_THRESHOLD)
            conf = sound_confidence(scores, rules)
            hit = conf >= limit
            held = self._held.pop(sound, None)
            if held is not None:
                best = max(held.confidence, conf) if hit else held.confidence
                self._last_emit[sound] = at
                out.append(Detection(sound, best, held.at))
                continue
            if not hit:
                continue
            last = self._last_emit.get(sound)
            if last is not None and at - last < self._debounce_s:
                continue
            self._held[sound] = Detection(sound, conf, at)
        # A sound disabled while held is dropped: only enabled sounds are ever reported.
        for sound in [s for s in self._held if s not in enabled]:
            del self._held[sound]
        return out

    def flush(self) -> list[Detection]:
        """Emit candidates still held for a window that will never come (stream ended)."""
        out = list(self._held.values())
        for d in out:
            self._last_emit[d.sound] = d.at
        self._held.clear()
        return out
