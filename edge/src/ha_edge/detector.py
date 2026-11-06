"""Turns per-window YAMNet scores into debounced, thresholded detections."""

from dataclasses import dataclass

import numpy as np

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

    def process(
        self, scores: np.ndarray, at: float, enabled: frozenset[str], playing: bool
    ) -> list[Detection]:
        scores = np.asarray(scores)
        expected = self._num_classes
        if scores.ndim != 1 or (expected is not None and scores.shape[0] != expected):
            raise ValueError(f"scores must be 1-D of length {expected}, got {scores.shape}")
        if scores.shape[0] < self._needed:
            raise ValueError(f"scores too short: {scores.shape[0]} < {self._needed}")
        limit = min(self._threshold + (self._boost if playing else 0.0), MAX_THRESHOLD)
        out: list[Detection] = []
        for sound, idxs in self._index.items():
            if sound not in enabled:
                continue
            conf = float(scores[idxs].max())
            if conf < limit:
                continue
            last = self._last_emit.get(sound)
            if last is not None and at - last < self._debounce_s:
                continue
            self._last_emit[sound] = at
            out.append(Detection(sound, conf, at))
        return out
