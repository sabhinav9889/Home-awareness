import builtins
import pathlib

import numpy as np
import pytest

pytestmark = pytest.mark.camera
pytest.importorskip("cv2", reason="needs the camera extra (opencv)")

from ha_edge.camera.capture import Frame
from ha_edge.camera.snapshot import encode_snapshot


def _gradient(w: int, h: int) -> np.ndarray:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    img = np.tile(x, (h, 1))
    return np.dstack([img, img[::-1], img]).astype(np.uint8)


def _dims(jpeg: bytes) -> tuple[int, int]:
    import cv2

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img.shape[1], img.shape[0]


def test_large_frame_downscaled_valid_jpeg():
    frame = Frame(_gradient(1920, 1080), captured_at=100.0)
    out = encode_snapshot(frame, now=100.2)
    assert out is not None
    assert out[:3] == b"\xff\xd8\xff" and out[-2:] == b"\xff\xd9"
    assert len(out) <= 300_000
    w, h = _dims(out)
    assert max(w, h) == 640 and (w, h) == (640, 360)


def test_small_frame_not_upscaled():
    out = encode_snapshot(Frame(_gradient(320, 240), 5.0), now=5.0)
    assert out is not None and _dims(out) == (320, 240)


def test_stale_frame_returns_none():
    frame = Frame(_gradient(320, 240), captured_at=10.0)
    assert encode_snapshot(frame, now=11.5) is None
    assert encode_snapshot(frame, now=11.5, max_age_s=2.0) is not None


def test_noise_that_cannot_fit_returns_none():
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
    assert encode_snapshot(Frame(noise, 1.0), now=1.0, max_bytes=20_000) is None


def test_quality_steps_down_to_fit():
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 256, (360, 640, 3), dtype=np.uint8)
    out = encode_snapshot(Frame(noise, 1.0), now=1.0, max_bytes=300_000)
    assert out is not None and len(out) <= 300_000


def test_nothing_written_to_disk(monkeypatch):
    import cv2

    def boom(*a, **k):
        raise AssertionError("snapshot code touched the disk")

    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(pathlib.Path, "write_bytes", boom)
    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    monkeypatch.setattr(cv2, "imwrite", boom)
    out = encode_snapshot(Frame(_gradient(1920, 1080), 1.0), now=1.0)
    assert out is not None


def _valid_or_none(out):
    assert out is None or (out[:2] == b"\xff\xd8" and out[-2:] == b"\xff\xd9")


@pytest.mark.parametrize(
    "image",
    [
        np.zeros((1, 20000, 3), np.uint8),
        np.zeros((20000, 1, 3), np.uint8),
        np.zeros((0, 0, 3), np.uint8),
        np.zeros((0,), np.uint8),
        np.zeros((240, 320), np.uint8),
        np.zeros((240, 320, 1), np.uint8),
        np.zeros((240, 320, 4), np.uint8),
        _gradient(640, 480)[::2, ::2],
        _gradient(640, 480)[:, ::-1],
    ],
    ids=["wide", "tall", "empty3d", "empty1d", "gray2d", "h-w-1", "bgra", "slice", "reversed"],
)
def test_degenerate_inputs_are_total(image):
    out = encode_snapshot(Frame(image, 1.0), now=1.0)
    _valid_or_none(out)
    if image.size and image.ndim >= 2:
        assert out is not None


def test_max_side_zero_returns_none():
    assert encode_snapshot(Frame(_gradient(64, 64), 1.0), now=1.0, max_side=0) is None


def test_lowered_quality_result_has_eoi():
    rng = np.random.default_rng(2)
    noise = rng.integers(0, 256, (360, 640, 3), dtype=np.uint8)
    full = encode_snapshot(Frame(noise, 1.0), now=1.0, max_bytes=10**9)
    out = encode_snapshot(Frame(noise, 1.0), now=1.0, max_bytes=len(full) - 1)
    assert out is not None and len(out) < len(full)
    assert out[:2] == b"\xff\xd8" and out[-2:] == b"\xff\xd9"
