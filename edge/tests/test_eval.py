import wave

import numpy as np
import pytest
from ha_edge.detector import Detector
from ha_edge.evaluation import (
    ESC50_MAP,
    compute_metrics,
    detections_from_scores,
    format_markdown,
    read_wav_16k,
    score_clip,
)
from ha_edge.labels import CATALOG_KEYS


def test_mapping_covers_only_catalog_keys():
    assert set(ESC50_MAP.values()) <= CATALOG_KEYS
    assert ESC50_MAP["crying_baby"] == "crying"
    assert ESC50_MAP["dog"] == "dog_barking"
    assert "door_wood_creaks" not in ESC50_MAP
    assert "clock_alarm" not in ESC50_MAP


def test_metrics_from_confusion():
    preds = [
        ("dog", {"dog_barking"}),  # TP
        ("dog", {"dog_barking"}),  # TP
        ("dog", set()),  # FN
        ("rain", {"dog_barking"}),  # FP
        ("rain", set()),  # TN
        ("clock_alarm", set()),  # TN
        ("siren", {"siren"}),
    ]
    m = compute_metrics(preds)["dog_barking"]
    assert (m.tp, m.fp, m.fn, m.positives, m.negatives) == (2, 1, 1, 3, 4)
    assert m.precision == pytest.approx(2 / 3)
    assert m.recall == pytest.approx(2 / 3)
    assert m.f1 == pytest.approx(2 / 3)
    assert m.fp_per_100_negatives == pytest.approx(25.0)


def test_metrics_zero_division_is_zero():
    m = compute_metrics([("dog", set()), ("rain", set())])["dog_barking"]
    assert (m.precision, m.recall, m.f1) == (0.0, 0.0, 0.0)


def test_metrics_only_for_mapped_sounds():
    assert set(compute_metrics([("rain", set())])) == set(ESC50_MAP.values())


class FakeClassifier:
    class_names = ["a", "b", "c"]

    def __init__(self, hot_window: int | None):
        self.calls = 0
        self.hot_window = hot_window

    def scores(self, window):
        i = self.calls
        self.calls += 1
        out = np.zeros(3, dtype=np.float32)
        if i == self.hot_window:
            out[1] = 0.9
        return out


def test_score_clip_windows_and_detects():
    clf = FakeClassifier(hot_window=3)
    det = Detector({"dog_barking": [1], "siren": [2]}, threshold=0.3, num_classes=3)
    found = score_clip(np.zeros(80000, dtype=np.float32), clf, det)
    assert clf.calls == 9
    assert found == {"dog_barking"}


def test_detections_from_scores_threshold():
    scores = [np.array([0, 0.4, 0], dtype=np.float32)]
    idx = {"dog_barking": [1]}
    assert detections_from_scores(scores, Detector(idx, 0.3), ) == {"dog_barking"}
    assert detections_from_scores(scores, Detector(idx, 0.5)) == set()


def test_read_wav_resamples_to_16k(tmp_path):
    p = tmp_path / "x.wav"
    tone = (np.sin(np.arange(44100) / 20) * 16000).astype(np.int16)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(tone.tobytes())
    x = read_wav_16k(p)
    assert x.dtype == np.float32
    assert len(x) == 16000
    assert np.abs(x).max() <= 1.0


def test_format_markdown_has_row_per_sound():
    md = format_markdown({0.30: compute_metrics([("dog", {"dog_barking"})])})
    assert "dog_barking" in md and "0.30" in md
