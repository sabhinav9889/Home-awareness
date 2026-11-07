import hashlib
import os
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx

from ha_edge import model
from ha_edge.model import ModelIntegrityError, YamnetClassifier, fetch_model

PAYLOAD = b"fake-model-bytes" * 100


@pytest.fixture
def patched_sha(monkeypatch):
    monkeypatch.setattr(model, "MODEL_SHA256", hashlib.sha256(PAYLOAD).hexdigest())


@respx.mock
def test_fetch_verifies_sha_and_rejects_mismatch(tmp_path, patched_sha):
    route = respx.get(model.MODEL_URL).mock(return_value=httpx.Response(200, content=PAYLOAD))
    path = fetch_model(tmp_path)
    assert path.read_bytes() == PAYLOAD
    assert route.called
    assert [p.name for p in tmp_path.iterdir()] == [model.MODEL_FILENAME]

    bad = tmp_path / "bad"
    respx.get(model.MODEL_URL).mock(return_value=httpx.Response(200, content=b"tampered"))
    with pytest.raises(ModelIntegrityError):
        fetch_model(bad)
    assert list(bad.iterdir()) == []


@respx.mock
def test_fetch_replaces_corrupt_existing_file(tmp_path, patched_sha):
    (tmp_path / model.MODEL_FILENAME).write_bytes(b"corrupt")
    respx.get(model.MODEL_URL).mock(return_value=httpx.Response(200, content=PAYLOAD))
    assert fetch_model(tmp_path).read_bytes() == PAYLOAD


@respx.mock
def test_fetch_is_noop_when_present(tmp_path, patched_sha):
    (tmp_path / model.MODEL_FILENAME).write_bytes(PAYLOAD)
    route = respx.get(model.MODEL_URL).mock(return_value=httpx.Response(500))
    assert fetch_model(tmp_path) == tmp_path / model.MODEL_FILENAME
    assert not route.called


@respx.mock
def test_fetch_http_error_leaves_no_temp(tmp_path, patched_sha):
    respx.get(model.MODEL_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


def _model_path() -> Path | None:
    env = os.environ.get("HA_EDGE_MODEL_PATH")
    p = Path(env) if env else model.default_model_dir() / model.MODEL_FILENAME
    return p if p.exists() else None


needs_model = pytest.mark.skipif(_model_path() is None, reason="YAMNet model not downloaded")


@pytest.fixture(scope="module")
def clf():
    return YamnetClassifier(_model_path())


@pytest.mark.model
@needs_model
def test_yamnet_scores_shape_and_range(clf):
    assert len(clf.class_names) == 521
    assert clf.class_names[0] == "Speech"
    s = clf.scores(np.zeros(15600, dtype=np.float32))
    assert s.shape == (521,) and s.dtype == np.float32
    assert s.min() >= 0.0 and s.max() <= 1.0


@pytest.mark.model
@needs_model
def test_yamnet_rejects_wrong_window_length(clf):
    with pytest.raises(ValueError):
        clf.scores(np.zeros(100, dtype=np.float32))


ALLOWED = {
    "Sine wave", "Beep, bleep", "Whistle", "Tuning fork", "Pure tone", "Hum",
    "Buzzer", "Ringtone", "Electronic tuner",
}  # fmt: skip


@pytest.mark.model
@needs_model
def test_yamnet_detects_synthetic_siren_or_tone_class(clf):
    t = np.arange(15600) / 16000
    wave = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    s = clf.scores(wave)
    top = clf.class_names[int(np.argmax(s))]
    assert top in ALLOWED, top


class _FakeInterp:
    def __init__(self):
        self.seen = None

    def set_tensor(self, index, value):
        self.seen = value.copy()

    def invoke(self):
        pass

    def get_tensor(self, index):
        return np.zeros((1, model.NUM_CLASSES), dtype=np.float32)


def test_scores_clips_window_to_unit_range():
    import threading

    clf = object.__new__(YamnetClassifier)
    clf._interp = _FakeInterp()
    clf._in, clf._out = 0, 1
    clf._lock = threading.Lock()
    window = np.linspace(-3.0, 3.0, model.WINDOW_SAMPLES, dtype=np.float32)
    clf.scores(window)
    assert clf._interp.seen.min() == -1.0 and clf._interp.seen.max() == 1.0


def _chime_note(freq: float, seconds: float) -> np.ndarray:
    """A decaying bell-like note: fundamental plus two harmonics."""
    t = np.arange(int(16000 * seconds)) / 16000
    tone = np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(4 * np.pi * freq * t)
    tone += 0.2 * np.sin(6 * np.pi * freq * t)
    return 0.5 * np.exp(-3.0 * t) * tone / 1.6


def _detected(clf, samples: np.ndarray) -> set[str]:
    from ha_edge.detector import Detector
    from ha_edge.evaluation import clip_scores, detections_from_scores
    from ha_edge.labels import build_index

    detector = Detector(build_index(clf.class_names), num_classes=len(clf.class_names))
    return detections_from_scores(clip_scores(samples.astype(np.float32), clf), detector)


@pytest.mark.model
@needs_model
def test_two_tone_ding_dong_chime_is_a_doorbell(clf):
    # E5 then C5, like a chime doorbell (the live debug signal). YAMNet hears it as
    # "Ding"/"Clang", not "Doorbell", so only the Ding rule catches it.
    chime = np.concatenate([_chime_note(659.25, 0.8), _chime_note(523.25, 1.2), np.zeros(8000)])
    assert _detected(clf, chime) == {"doorbell"}


@pytest.mark.model
@needs_model
def test_pure_tone_detects_nothing(clf):
    t = np.arange(3 * 16000) / 16000
    assert _detected(clf, 0.5 * np.sin(2 * np.pi * 1000 * t)) == set()
