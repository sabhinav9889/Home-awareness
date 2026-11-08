import shutil
import subprocess
import threading
import time

import numpy as np
import pytest

pytestmark = pytest.mark.camera
pytest.importorskip("cv2", reason="needs the camera extra (opencv)")

from ha_edge.camera.capture import CameraBusy, CameraSource, FileSource


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("clip") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=8",
            "-t",
            "3",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


def test_file_source_paces_at_fps(clip):
    src = FileSource(clip, loop=True, fps=8)
    try:
        t0 = time.monotonic()
        frames = [src.read() for _ in range(9)]
        elapsed = time.monotonic() - t0
    finally:
        src.close()
    assert all(f is not None and f.image.shape == (240, 320, 3) for f in frames)
    assert 0.7 <= elapsed <= 2.0  # 8 intervals at 8 fps is about 1 s
    stamps = [f.captured_at for f in frames]
    assert stamps == sorted(stamps)


def test_file_source_ends_without_loop(clip):
    src = FileSource(clip, loop=False, fps=1000)
    n = 0
    while src.read() is not None:
        n += 1
        assert n < 100
    src.close()
    assert 20 <= n <= 28  # 3 s at 8 fps


def test_file_source_loops(clip):
    src = FileSource(clip, loop=True, fps=1000)
    try:
        assert all(src.read() is not None for _ in range(40))
    finally:
        src.close()


class FakeCapture:
    """Numbered frames at ~200 fps; pixel [0,0,0] holds the frame number."""

    def __init__(self, opened=True, produce=True, interval=0.005):
        self._opened = opened
        self._produce = produce
        self._interval = interval
        self.n = 0
        self.released = False
        self.props = {}

    def isOpened(self):
        return self._opened and not self.released

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def read(self):
        if self.released:
            return False, None
        time.sleep(self._interval)
        if not self._produce:
            return False, None
        self.n += 1
        img = np.zeros((4, 4, 3), np.uint8)
        img[0, 0, 0] = self.n % 256
        img[0, 1, 0] = self.n // 256
        return True, img

    def release(self):
        self.released = True


def _num(frame):
    return int(frame.image[0, 0, 0]) + 256 * int(frame.image[0, 1, 0])


def test_camera_source_sets_buffer_size_one_and_closes():
    cap = FakeCapture()
    src = CameraSource(0, capture_factory=lambda i: cap)
    assert src.opened
    import cv2

    assert cap.props.get(cv2.CAP_PROP_BUFFERSIZE) == 1
    thread = src._thread
    src.close()
    assert cap.released and not thread.is_alive() and not src.opened


def test_slow_consumer_gets_newest_frame_not_stale():
    cap = FakeCapture()
    src = CameraSource(0, capture_factory=lambda i: cap)
    try:
        first = src.read()
        time.sleep(0.5)  # ~100 frames grabbed meanwhile
        t_before = time.monotonic()
        second = src.read()
    finally:
        src.close()
    assert _num(second) > _num(first) + 50
    assert t_before - second.captured_at < 0.1  # grabbed just now, not 0.5 s ago


def test_read_never_returns_same_frame_twice():
    cap = FakeCapture(interval=0.02)
    src = CameraSource(0, capture_factory=lambda i: cap)
    try:
        nums = [_num(src.read()) for _ in range(5)]
    finally:
        src.close()
    assert nums == sorted(set(nums))


def test_read_returns_none_after_timeout_without_new_frame():
    cap = FakeCapture()
    src = CameraSource(0, capture_factory=lambda i: cap, read_timeout_s=0.2)
    try:
        assert src.read() is not None
        cap._produce = False
        t0 = time.monotonic()
        assert src.read() is None
        assert 0.15 <= time.monotonic() - t0 < 1.0
    finally:
        src.close()


def test_open_failure_raises_camera_busy():
    cap = FakeCapture(opened=False)
    with pytest.raises(CameraBusy):
        CameraSource(0, capture_factory=lambda i: cap)
    assert cap.released


def test_no_first_frame_raises_camera_busy_and_releases():
    cap = FakeCapture(produce=False)
    made = []

    def factory(i):
        return cap

    with pytest.raises(CameraBusy):
        CameraSource(0, capture_factory=factory, first_frame_timeout_s=0.3)
    assert cap.released
    for t in threading.enumerate():
        made.append(t.name)
    assert "ha-camera-grab" not in made


class BlockingCapture(FakeCapture):
    """read() blocks until `gate` is set; records release-while-reading and counts."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.in_read = False
        self.release_calls = 0
        self.released_during_read = False
        self.read_started = threading.Event()

    def read(self):
        self.in_read = True
        self.read_started.set()
        self.gate.wait(10)
        self.in_read = False
        return False, None

    def release(self):
        self.release_calls += 1
        if self.in_read:
            self.released_during_read = True
        self.released = True


def test_close_with_blocked_read_does_not_release_during_read():
    cap = BlockingCapture()
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        if calls["n"] == 1:
            return True, np.zeros((4, 4, 3), np.uint8)
        return BlockingCapture.read(cap)  # blocks until cap.gate is set

    cap.read = read
    cap.isOpened = lambda: True
    src = CameraSource(0, capture_factory=lambda i: cap)
    assert cap.read_started.wait(2)
    t0 = time.monotonic()
    src.close()
    assert time.monotonic() - t0 < 2.5
    assert cap.release_calls == 0 and not cap.released_during_read
    assert src.read() is None
    cap.gate.set()
    src._thread.join(2)
    assert not src._thread.is_alive()
    assert cap.release_calls == 1 and not cap.released_during_read
    src.close()
    assert cap.release_calls == 1


def test_normal_close_releases_exactly_once():
    cap = FakeCapture()
    calls = []
    orig = cap.release
    cap.release = lambda: (calls.append(1), orig())[1]
    src = CameraSource(0, capture_factory=lambda i: cap)
    src.close()
    src.close()
    src._thread.join(2)
    assert calls == [1]


def test_read_after_close_returns_none_immediately():
    src = CameraSource(0, capture_factory=lambda i: FakeCapture(), read_timeout_s=5)
    src.close()
    t0 = time.monotonic()
    assert src.read() is None
    assert time.monotonic() - t0 < 0.5


def test_grab_thread_exception_marks_dead_and_read_returns_none():
    cap = FakeCapture()
    n = {"c": 0}
    orig = cap.read

    def read():
        n["c"] += 1
        if n["c"] > 3:
            raise RuntimeError("driver blew up")
        return orig()

    cap.read = read
    src = CameraSource(0, capture_factory=lambda i: cap, read_timeout_s=5)
    try:
        src._thread.join(2)
        assert not src._thread.is_alive()
        assert not src.opened
        t0 = time.monotonic()
        while src.read() is not None:  # drain at most the one pending frame
            pass
        assert time.monotonic() - t0 < 1.0
        assert cap.released
    finally:
        src.close()


def test_release_when_set_raises():
    cap = FakeCapture()

    def bad_set(prop, value):
        raise RuntimeError("no such property")

    cap.set = bad_set
    with pytest.raises(RuntimeError):
        CameraSource(0, capture_factory=lambda i: cap)
    assert cap.released
