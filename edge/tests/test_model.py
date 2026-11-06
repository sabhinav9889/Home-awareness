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
