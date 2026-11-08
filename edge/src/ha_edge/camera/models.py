"""MediaPipe model files (Apache-2.0): pinned SHA-256, verified download, never committed."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import httpx

from ha_edge.model import ModelIntegrityError, _sha256_file, default_model_dir

POSE_FILE = "pose_landmarker_lite.task"
OBJECT_FILE = "efficientdet_lite0.tflite"
POSE_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
)
OBJECT_URL = (
    "https://storage.googleapis.com/mediapipe-models/object_detector/"
    "efficientdet_lite0/float16/latest/efficientdet_lite0.tflite"
)
# Measured 2026-11-08 from two independent downloads (5,777,746 and 7,254,339 bytes).
POSE_SHA256 = "59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a"
OBJECT_SHA256 = "4b59100025bea1235a84c1038879a6cccc9f6c49f5e41144e91e74d99e780993"

__all__ = [
    "OBJECT_FILE",
    "OBJECT_SHA256",
    "OBJECT_URL",
    "POSE_FILE",
    "POSE_SHA256",
    "POSE_URL",
    "ModelIntegrityError",
    "default_model_dir",
    "fetch_models",
]


def _fetch_one(client: httpx.Client, url: str, dest: Path, sha256: str) -> None:
    if dest.exists() and _sha256_file(dest) == sha256:
        return
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as f, client.stream("GET", url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != sha256:
            raise ModelIntegrityError(
                f"{dest.name} SHA-256 mismatch: expected {sha256}, got {h.hexdigest()}"
            )
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def fetch_models(dest_dir: Path | None = None, http: httpx.Client | None = None) -> Path:
    """Download both models into dest_dir (default: the YAMNet model dir), verifying each
    SHA-256. Files that are already valid are left alone. Returns the directory."""
    dest_dir = Path(dest_dir) if dest_dir is not None else default_model_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    client = http or httpx.Client(follow_redirects=True, timeout=60.0)
    try:
        _fetch_one(client, POSE_URL, dest_dir / POSE_FILE, POSE_SHA256)
        _fetch_one(client, OBJECT_URL, dest_dir / OBJECT_FILE, OBJECT_SHA256)
    finally:
        if http is None:
            client.close()
    return dest_dir
