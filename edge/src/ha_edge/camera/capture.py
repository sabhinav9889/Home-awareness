"""Frame sources. Frames live in memory only; nothing here writes to disk.

cv2 is imported lazily so this module imports without the camera extra.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np


class CameraBusy(RuntimeError):
    """The camera could not be opened or produced no frame (busy or unplugged)."""


@dataclass
class Frame:
    image: np.ndarray  # BGR, uint8
    captured_at: float  # time.monotonic() when the frame was grabbed


class FrameSource(Protocol):
    def read(self) -> Frame | None: ...

    def close(self) -> None: ...


def _open_v4l2(index: int) -> Any:
    import cv2

    return cv2.VideoCapture(index, cv2.CAP_V4L2)


class CameraSource:
    """Live camera. A daemon thread grabs continuously into a single slot that
    each new frame overwrites, so read() never returns an old buffered frame."""

    def __init__(
        self,
        index: int,
        *,
        capture_factory: Callable[[int], Any] = _open_v4l2,
        read_timeout_s: float = 1.0,
        first_frame_timeout_s: float = 3.0,
    ) -> None:
        import cv2

        self._read_timeout_s = read_timeout_s
        self._cond = threading.Condition()
        self._slot: Frame | None = None
        self._fresh = False
        self._stop = threading.Event()
        self._cap = capture_factory(index)
        if not self._cap.isOpened():
            self._cap.release()
            raise CameraBusy(f"cannot open camera {index} (busy or unplugged)")
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._thread = threading.Thread(target=self._grab_loop, daemon=True, name="ha-camera-grab")
        self._thread.start()
        if self._wait_fresh(first_frame_timeout_s) is None:
            self.close()
            raise CameraBusy(f"camera {index} gave no frame in {first_frame_timeout_s:g} s")

    @property
    def opened(self) -> bool:
        return not self._stop.is_set() and self._cap.isOpened()

    def _grab_loop(self) -> None:
        while not self._stop.is_set():
            ok, img = self._cap.read()
            if self._stop.is_set():
                break
            if not ok or img is None:
                time.sleep(0.01)
                continue
            frame = Frame(img, time.monotonic())
            with self._cond:
                self._slot = frame
                self._fresh = True
                self._cond.notify_all()

    def _wait_fresh(self, timeout: float) -> Frame | None:
        with self._cond:
            if not self._cond.wait_for(lambda: self._fresh, timeout):
                return None
            self._fresh = False
            return self._slot

    def read(self) -> Frame | None:
        """Newest frame not yet returned, or None after read_timeout_s without one."""
        return self._wait_fresh(self._read_timeout_s)

    def close(self) -> None:
        self._stop.set()
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._cap.release()
        with self._cond:
            self._cond.notify_all()


class FileSource:
    """Plays a video file as if it were a camera (tests and dry runs; no device)."""

    def __init__(self, path: str | Path, loop: bool = True, fps: float = 4) -> None:
        import cv2

        self._cap = cv2.VideoCapture(str(path))
        if not self._cap.isOpened():
            self._cap.release()
            raise CameraBusy(f"cannot open video file {path}")
        self._loop = loop
        self._interval = 1.0 / fps
        self._next_at: float | None = None

    def read(self) -> Frame | None:
        now = time.monotonic()
        if self._next_at is not None and now < self._next_at:
            time.sleep(self._next_at - now)
        self._next_at = max(now, self._next_at or now) + self._interval
        ok, img = self._cap.read()
        if not ok and self._loop:
            self._cap.set(1, 0)  # CAP_PROP_POS_FRAMES
            ok, img = self._cap.read()
        if not ok or img is None:
            return None
        return Frame(img, time.monotonic())

    def close(self) -> None:
        self._cap.release()
