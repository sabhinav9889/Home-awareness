"""Accuracy-evaluation helpers: ESC-50 mapping, clip scoring and per-sound metrics."""

from __future__ import annotations

import wave
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ha_edge.audio import SAMPLE_RATE, Windower, resample_linear
from ha_edge.detector import Detector

# ESC-50 category -> catalog key. Every other ESC-50 category (including
# door_wood_creaks and clock_alarm) is a negative for every sound.
ESC50_MAP: dict[str, str] = {
    "crying_baby": "crying",
    "dog": "dog_barking",
    "glass_breaking": "glass_breaking",
    "door_wood_knock": "knocking",
    "siren": "siren",
    "coughing": "coughing",
}

# ESC-50 classes that sound like a doorbell or breaking glass but are not (or, for
# glass_breaking, are the positives): their per-class detection counts show where false
# alarms come from.
PROBE_CATEGORIES: tuple[str, ...] = (
    "church_bells",
    "clock_alarm",
    "glass_breaking",
    "can_opening",
    "door_wood_knock",
)


@dataclass(frozen=True)
class SoundMetrics:
    sound: str
    tp: int
    fp: int
    fn: int
    positives: int
    negatives: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / self.positives if self.positives else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def fp_per_100_negatives(self) -> float:
        return 100.0 * self.fp / self.negatives if self.negatives else 0.0


def compute_metrics(
    predictions: Iterable[tuple[str, set[str]]],
    mapping: dict[str, str] = ESC50_MAP,
) -> dict[str, SoundMetrics]:
    """predictions: (esc50_category, detected_sounds) per clip."""
    preds = list(predictions)
    out: dict[str, SoundMetrics] = {}
    for sound in dict.fromkeys(mapping.values()):
        tp = fp = fn = pos = neg = 0
        for category, detected in preds:
            hit = sound in detected
            if mapping.get(category) == sound:
                pos += 1
                tp += hit
                fn += not hit
            else:
                neg += 1
                fp += hit
        out[sound] = SoundMetrics(sound, tp, fp, fn, pos, neg)
    return out


def detections_by_category(
    predictions: Iterable[tuple[str, set[str]]],
    sounds: Sequence[str],
    categories: Sequence[str] = PROBE_CATEGORIES,
) -> dict[str, dict[str, tuple[int, int]]]:
    """For each sound and ESC-50 category: (clips where the sound fired, clips)."""
    preds = list(predictions)
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for sound in sounds:
        row: dict[str, tuple[int, int]] = {}
        for cat in categories:
            clips = [detected for category, detected in preds if category == cat]
            row[cat] = (sum(sound in d for d in clips), len(clips))
        out[sound] = row
    return out


def format_category_markdown(
    results: dict[float, dict[str, dict[str, tuple[int, int]]]],
) -> str:
    """Rows of sound x threshold, a column per category, cells "fired/clips"."""
    first = next(iter(results.values()), {})
    categories = list(next(iter(first.values()), {}))
    lines = [
        "| Sound | Threshold | " + " | ".join(categories) + " |",
        "|---|---|" + "---|" * len(categories),
    ]
    for sound in first:
        for thr, by_sound in sorted(results.items()):
            cells = " | ".join(f"{n}/{total}" for n, total in by_sound[sound].values())
            lines.append(f"| {sound} | {thr:.2f} | {cells} |")
    return "\n".join(lines) + "\n"


def read_wav_16k(path: Path) -> np.ndarray:
    """Read a 16-bit PCM WAV, mix to mono, resample to 16 kHz float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit PCM supported")
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    return resample_linear(x, rate, SAMPLE_RATE).astype(np.float32)


def clip_scores(samples_16k: np.ndarray, classifier) -> list[np.ndarray]:
    windows = Windower().push(samples_16k)
    return [classifier.scores(w) for w in windows]


def detections_from_scores(
    scores: Sequence[np.ndarray],
    detector: Detector,
    enabled: frozenset[str] | None = None,
) -> set[str]:
    enabled = enabled if enabled is not None else frozenset(detector._index)
    hop_s = 7800 / SAMPLE_RATE
    found: set[str] = set()
    for i, s in enumerate(scores):
        found.update(d.sound for d in detector.process(s, i * hop_s, enabled, playing=False))
    found.update(d.sound for d in detector.flush())  # held through the clip's last window
    return found


def score_clip(samples_16k: np.ndarray, classifier, detector: Detector) -> set[str]:
    """Sounds detected anywhere in the clip (all sounds enabled, not playing)."""
    return detections_from_scores(clip_scores(samples_16k, classifier), detector)


def format_markdown(results: dict[float, dict[str, SoundMetrics]]) -> str:
    lines = [
        "| Sound | Threshold | Clips | Precision | Recall | F1 | FP / 100 negatives |",
        "|---|---|---|---|---|---|---|",
    ]
    sounds = next(iter(results.values())).keys() if results else []
    for sound in sounds:
        for thr, metrics in sorted(results.items()):
            m = metrics[sound]
            lines.append(
                f"| {sound} | {thr:.2f} | {m.positives} | {m.precision:.2f} | "
                f"{m.recall:.2f} | {m.f1:.2f} | {m.fp_per_100_negatives:.1f} |"
            )
    return "\n".join(lines) + "\n"
