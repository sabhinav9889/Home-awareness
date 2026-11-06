"""Turns per-window YAMNet scores into debounced, thresholded detections."""

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

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
        index: dict[str, list[int]],
        threshold: float = 0.30,
        boost: float = 0.20,
        debounce_s: float = 2.0,
        num_classes: int | None = None,
    ) -> None:
        self._index = index
        self._threshold = threshold
        self._boost = boost
        self._debounce_s = debounce_s
        self._num_classes = num_classes
        self._needed = max((i for idx in index.values() for i in idx), default=-1) + 1
        self._last_emit: dict[str, float] = {}

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
        for sound, idxs in self._index.items():
            if sound not in enabled:
                continue
            base = self._base(sound, thresholds)
            boosted = min(base + self._boost, MAX_THRESHOLD)
            if base <= float(scores[idxs].max()) < boosted:
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
        for sound, idxs in self._index.items():
            if sound not in enabled:
                continue
            limit = min(self._base(sound, thresholds) + boost, MAX_THRESHOLD)
            conf = float(scores[idxs].max())
            if conf < limit:
                continue
            last = self._last_emit.get(sound)
            if last is not None and at - last < self._debounce_s:
                continue
            self._last_emit[sound] = at
            out.append(Detection(sound, conf, at))
        return out
