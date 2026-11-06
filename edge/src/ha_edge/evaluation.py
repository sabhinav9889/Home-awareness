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
