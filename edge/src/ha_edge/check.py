"""``ha-sensor check``: what does the sensor hear right now?

Prints, for each window (about twice a second): the input level and peak, a CLIPPING
marker, YAMNet's top three classes, and the catalog sounds scoring at least 0.05, with
a check mark when the sensor would report them under the current thresholds. Ends with
a summary and one line of advice about the input level. Nothing is recorded or sent:
audio stays in memory and only the numbers are printed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np

from ha_edge.audio import SAMPLE_RATE, Windower, clip_ratio
from ha_edge.detector import Detector
from ha_edge.labels import build_index, sound_confidence

SHOW_FROM = 0.05  # catalog sounds below this are left out of the per-window line
CLIPPING_RATIO = 0.01  # a window with at least 1 % clipped samples counts as clipping
QUIET_PEAK = 0.1  # the loudest sound never got above this: the level is too low
LOWER_HINT = "wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.3"
RAISE_HINT = "wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.7"
TICK = "✓"


@dataclass
class CheckSummary:
    windows: int = 0
    loudest_level: float = 0.0
    loudest_peak: float = 0.0
    clipping_windows: int = 0
    best: dict[str, float] = field(default_factory=dict)  # every catalog sound
    advice: str = ""


def advice(loudest_peak: float, clipping_windows: int) -> str:
    if clipping_windows:
        return (
            "Lower the input level: the microphone clipped, which distorts loud sounds "
            f"(e.g. {LOWER_HINT})."
        )
    if loudest_peak < QUIET_PEAK:
        return (
            f"The loudest sound peaked at only {loudest_peak:.2f}. If you made the sound "
            f"during the check, raise the input level or move the laptop closer "
            f"(e.g. {RAISE_HINT})."
        )
    return "The input level looks right."


class _Marker:
    """Whether a sound's score would make the sensor report it."""

    def __init__(
        self, detector: Detector, thresholds: Mapping[str, float], enabled: frozenset[str] | None
    ):
        self._detector = detector
        self._thresholds = thresholds
        self._enabled = enabled

    def __call__(self, sound: str, score: float) -> str:
        if self._enabled is not None and sound not in self._enabled:
            return " (off)"
        return f" {TICK}" if score >= self._detector.threshold(sound, self._thresholds) else ""


def _sounds(scores: Mapping[str, float], mark: _Marker) -> str:
    shown = sorted(
        ((k, v) for k, v in scores.items() if v >= SHOW_FROM), key=lambda kv: (-kv[1], kv[0])
    )
    return ", ".join(f"{k} {v:.2f}{mark(k, v)}" for k, v in shown) or "-"


async def run_check(
    classifier,
    mic,
    *,
    seconds: float,
    thresholds: Mapping[str, float],
    enabled: frozenset[str] | None = None,
    out: Callable[[str], None] = print,
) -> CheckSummary:
    """Listen for ``seconds`` of audio; print a line per window and a summary.

    ``thresholds`` are the server's per-sound thresholds ({} for the default 0.30);
    ``enabled`` the sounds someone turned on (None when unknown: not paired).
    Raises ``MicLost`` if the microphone goes away.
    """
    names = list(classifier.class_names)
    index = build_index(names)
    mark = _Marker(Detector(index, num_classes=len(names)), thresholds, enabled)
    summary = CheckSummary(best=dict.fromkeys(index, 0.0))
    windower = Windower()
    needed = int(seconds * SAMPLE_RATE)
    received = 0
    async with mic:
        async for chunk in mic:
            received += len(chunk)
            for window in windower.push(chunk):
                level = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
                peak = float(np.max(np.abs(window)))
                clipped = clip_ratio(window)
                scores = await asyncio.to_thread(classifier.scores, window)
                top = np.argsort(scores)[::-1][:3]
                tops = ", ".join(f"{names[i]} {scores[i]:.2f}" for i in top)
                ours = {k: sound_confidence(scores, rules) for k, rules in index.items()}
                summary.windows += 1
                summary.loudest_level = max(summary.loudest_level, level)
                summary.loudest_peak = max(summary.loudest_peak, peak)
                if clipped >= CLIPPING_RATIO:
                    summary.clipping_windows += 1
                for k, v in ours.items():
                    summary.best[k] = max(summary.best[k], v)
                flag = f"CLIPPING {clipped:.0%}" if clipped >= CLIPPING_RATIO else ""
                out(
                    f"level {level:5.3f} peak {peak:4.2f} {flag:<12} | {tops} | "
                    f"{_sounds(ours, mark)}"
                )
            if received >= needed:
                break
    summary.advice = advice(summary.loudest_peak, summary.clipping_windows)
    out("")
    out(f"Summary of {summary.windows} windows:")
    out(f"Loudest: level {summary.loudest_level:.3f}, peak {summary.loudest_peak:.2f}")
    out(f"Clipping: {summary.clipping_windows} of {summary.windows} windows")
    best = _sounds(summary.best, mark)
    out(f"Best scores: {best if best != '-' else 'no catalog sound reached 0.05'}")
    out(f"Advice: {summary.advice}")
    return summary
