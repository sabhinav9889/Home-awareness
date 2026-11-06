"""YAMNet classifier (TFLite) and verified model download."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
import threading
from importlib import resources
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np
import platformdirs

MODEL_URL = (
    "https://storage.googleapis.com/download.tensorflow.org/models/tflite/task_library/"
    "audio_classification/android/lite-model_yamnet_classification_tflite_1.tflite"
)
MODEL_SHA256 = "10c95ea3eb9a7bb4cb8bddf6feb023250381008177ac162ce169694d05c317de"
CLASS_MAP_URL = (
    "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/"
    "yamnet/yamnet_class_map.csv"
)
MODEL_FILENAME = "yamnet.tflite"
WINDOW_SAMPLES = 15600
NUM_CLASSES = 521


class ModelIntegrityError(Exception):
    """Downloaded model did not match the pinned SHA-256."""


def default_model_dir() -> Path:
    return Path(platformdirs.user_cache_dir("home-awareness"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_model(dest_dir: Path | None = None, *, http: httpx.Client | None = None) -> Path:
    """Download the model into dest_dir, verifying SHA-256. No-op if already valid."""
    dest_dir = Path(dest_dir) if dest_dir is not None else default_model_dir()
    dest = dest_dir / MODEL_FILENAME
    if dest.exists() and _sha256_file(dest) == MODEL_SHA256:
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    client = http or httpx.Client(follow_redirects=True, timeout=60.0)
    fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix=".yamnet-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as f, client.stream("GET", MODEL_URL) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != MODEL_SHA256:
            raise ModelIntegrityError(
                f"model SHA-256 mismatch: expected {MODEL_SHA256}, got {h.hexdigest()}"
            )
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if http is None:
            client.close()
    return dest


class Classifier(Protocol):
    class_names: list[str]

    def scores(self, window: np.ndarray) -> np.ndarray: ...


def load_class_names() -> list[str]:
    text = (resources.files("ha_edge") / "data" / "yamnet_class_map.csv").read_text("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    rows.sort(key=lambda r: int(r["index"]))
    return [r["display_name"] for r in rows]


class YamnetClassifier:
    def __init__(self, model_path: Path) -> None:
        from ai_edge_litert.interpreter import Interpreter

        self.class_names = load_class_names()
        self._interp = Interpreter(model_path=str(model_path))
        inp = self._interp.get_input_details()[0]
        if tuple(inp["shape"]) != (WINDOW_SAMPLES,):
            self._interp.resize_tensor_input(inp["index"], [WINDOW_SAMPLES])
        self._interp.allocate_tensors()
        self._in = self._interp.get_input_details()[0]["index"]
        self._out = self._interp.get_output_details()[0]["index"]
        n = int(np.prod(self._interp.get_output_details()[0]["shape"]))
        if n != NUM_CLASSES or len(self.class_names) != NUM_CLASSES:
            raise ValueError(f"expected {NUM_CLASSES} classes, model has {n}")
        self._lock = threading.Lock()

    def scores(self, window: np.ndarray) -> np.ndarray:
        window = np.asarray(window, dtype=np.float32)
        if window.shape != (WINDOW_SAMPLES,):
            raise ValueError(f"window must have shape ({WINDOW_SAMPLES},), got {window.shape}")
        with self._lock:
            self._interp.set_tensor(self._in, window)
            self._interp.invoke()
            out = self._interp.get_tensor(self._out)
        return np.array(out, dtype=np.float32).reshape(NUM_CLASSES)
