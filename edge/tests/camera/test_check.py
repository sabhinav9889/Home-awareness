import httpx
import numpy as np
import respx

from ha_edge import cli, config
from ha_edge.camera import check
from ha_edge.camera import cli as camera_cli
from ha_edge.camera.capture import CameraBusy, Frame
from ha_edge.camera.detectors import Detection
from ha_edge.camera.devices import CameraInfo
from ha_edge.camera.pose_rules import Landmarks


class Src:
    def __init__(self, level=128, frames=1000):
        self.level, self.frames, self.closed = level, frames, False

    def read(self):
        if self.frames <= 0:
            return None
        self.frames -= 1
        return Frame(np.full((8, 8, 3), self.level, np.uint8), 0.0)

    def close(self):
        self.closed = True


class Pose:
    def __init__(self, lm):
        self.lm, self.closed = lm, False

    def torso(self, frame):
        return self.lm

    def close(self):
        self.closed = True


class Objs:
    def __init__(self, dets=()):
        self.dets, self.closed = list(dets), False

    def detect(self, frame):
        return list(self.dets)

    def close(self):
        self.closed = True


UP = Landmarks((0.45, 0.3, 0.9), (0.55, 0.3, 0.9), (0.45, 0.7, 0.9), (0.55, 0.7, 0.9))


class Time:
    t = 0.0

    def clock(self):
        return self.t

    def sleep(self, d):
        self.t += d


def go(src, pose, objs, **kw):
    t, lines = Time(), []
    s = check.run_check(
        src, pose, objs, seconds=2, fps=4, clock=t.clock, sleep=t.sleep, out=lines.append, **kw
    )
    return s, lines


def test_lines_show_angle_person_guarding_and_would_send():
    s, lines = go(Src(), Pose(UP), Objs([Detection("person", 0.8), Detection("knife", 0.9)]))
    assert s.frames == 8
    assert "torso 0 deg" in lines[0] and "person 0.80" in lines[0]
    assert "guarding yes" in lines[0] and "would send: weapon_seen" in lines[0]
    assert any("person_away" in line and "weapon_seen" in line for line in lines[:8])
    assert "Nothing was sent" in lines[-2]


def test_no_body_and_advice():
    _s, lines = go(Src(), Pose(None), Objs())
    assert "no body" in lines[0]
    assert "No body was visible" in lines[-1]


def test_too_dark_advice():
    _s, lines = go(Src(level=5), Pose(UP), Objs())
    assert "too dark" in lines[-1]


def test_no_frames_means_busy_advice():
    s, lines = go(Src(frames=0), Pose(UP), Objs())
    assert s.frames == 0 and "busy or unplugged" in lines[-1]


def test_camera_busy_while_reading():
    class Bad(Src):
        def read(self):
            raise CameraBusy("busy")

    _s, lines = go(Bad(), Pose(UP), Objs())
    assert "busy or unplugged" in lines[-1]


def test_guarding_off_config_shown_and_no_person_event():
    from ha_edge.api import DeviceConfig

    dc = DeviceConfig(frozenset({"person_away"}), 60, {}, False, True)
    _s, lines = go(Src(), Pose(UP), Objs([Detection("person", 0.9)]), cfg=dc)
    assert "guarding no" in lines[0] and "person 0.90" in lines[0]
    assert all("would send: -" in line for line in lines[:8])


def test_cli_check_sends_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    config.save_camera(config.CameraConfig("http://srv", 1, "key", "Cam", 0))
    monkeypatch.setattr(
        "ha_edge.camera.devices.list_cameras", lambda: [CameraInfo(0, "Cam", "/dev/video0")]
    )
    src, pose, objs = Src(frames=3), Pose(UP), Objs([Detection("person", 0.9)])
    monkeypatch.setattr(camera_cli, "_open_camera", lambda i: src)
    monkeypatch.setattr(camera_cli, "_load_models", lambda: (pose, objs))
    monkeypatch.setattr(check.time, "sleep", lambda d: None)
    with respx.mock(base_url="http://srv", assert_all_called=False) as m:
        m.get("/device/config").respond(
            200,
            json={
                "enabled_sounds": ["person_away"],
                "heartbeat_interval_s": 60,
                "thresholds": {"person_away": 0.5},
                "guarding": True,
                "send_snapshots": True,
            },
        )
        events = m.post("/device/events").respond(202, json={})
        hb = m.post("/device/heartbeat").respond(200, json={})
        assert camera_cli.main(["check", "--seconds", "0.5"]) == cli.EXIT_OK
        assert not events.called and not hb.called
        assert all(c.request.method == "GET" for c in m.calls)
    out = capsys.readouterr().out
    assert "would send: person_away" in out and "Nothing was sent" in out
    assert src.closed and pose.closed and objs.closed


def test_cli_check_server_unreachable_falls_back_to_defaults(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    config.save_camera(config.CameraConfig("http://srv", 1, "key", "Cam", 0))
    monkeypatch.setattr(
        "ha_edge.camera.devices.list_cameras", lambda: [CameraInfo(0, "Cam", "/dev/video0")]
    )
    monkeypatch.setattr(camera_cli, "_open_camera", lambda i: Src(frames=2))
    monkeypatch.setattr(camera_cli, "_load_models", lambda: (Pose(UP), Objs()))
    monkeypatch.setattr(check.time, "sleep", lambda d: None)
    with respx.mock(base_url="http://srv") as m:
        m.get("/device/config").mock(side_effect=httpx.ConnectError("down"))
        assert camera_cli.main(["check", "--seconds", "0.5"]) == cli.EXIT_OK
    assert "using defaults" in capsys.readouterr().out


def test_cli_check_camera_busy_exits_with_advice(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        "ha_edge.camera.devices.list_cameras", lambda: [CameraInfo(0, "Cam", "/dev/video0")]
    )
    pose, objs = Pose(UP), Objs()

    def busy(i):
        raise CameraBusy("busy")

    monkeypatch.setattr(camera_cli, "_open_camera", busy)
    monkeypatch.setattr(camera_cli, "_load_models", lambda: (pose, objs))
    assert camera_cli.main(["check"]) == cli.EXIT_ERROR
    assert "busy or unplugged" in capsys.readouterr().out
    assert pose.closed and objs.closed


def test_cli_run_prints_watching_notice(monkeypatch, tmp_path, capsys):
    import asyncio

    from ha_edge.camera import runner

    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    config.save_camera(config.CameraConfig("http://srv", 1, "key"))
    monkeypatch.setattr(
        "ha_edge.camera.devices.list_cameras", lambda: [CameraInfo(2, "Cam", "/dev/video2")]
    )
    monkeypatch.setattr(camera_cli, "_load_models", lambda: (Pose(UP), Objs()))

    async def fake_run(client, source, pose, objects, *, stop):
        return 0

    monkeypatch.setattr(runner, "run_camera", fake_run)
    assert camera_cli.main(["run"]) == 0
    assert camera_cli.WATCHING in capsys.readouterr().out
    saved = config.load_camera()
    assert (saved.camera_name, saved.camera_index) == ("Cam", 2)
    assert asyncio


def test_cli_run_not_paired(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert camera_cli.main(["run"]) == cli.EXIT_NOT_PAIRED


# ---- _load_models (the real one) ----


def _model_dir(tmp_path, monkeypatch):
    from ha_edge.camera import models

    d = tmp_path / "models"
    d.mkdir()
    (d / models.POSE_FILE).write_bytes(b"x")
    (d / models.OBJECT_FILE).write_bytes(b"x")
    monkeypatch.setattr(models, "default_model_dir", lambda: d)
    return d


def test_load_models_uses_the_detector_wrappers(monkeypatch, tmp_path):
    import pytest

    pytest.importorskip("cv2")
    from ha_edge.camera import detectors, models

    d = _model_dir(tmp_path, monkeypatch)

    class P:
        def __init__(self, path):
            self.path = path

    class O(P):
        pass

    monkeypatch.setattr(detectors, "MediapipePose", P)
    monkeypatch.setattr(detectors, "MediapipeObjects", O)
    pose, objs = camera_cli._load_models()
    assert pose.path == d / models.POSE_FILE and objs.path == d / models.OBJECT_FILE


def test_load_models_failure_is_one_line_and_closes_pose(monkeypatch, tmp_path, capsys):
    import pytest

    pytest.importorskip("cv2")
    from ha_edge.camera import detectors

    _model_dir(tmp_path, monkeypatch)
    closed = []

    class P:
        def __init__(self, path):
            pass

        def close(self):
            closed.append(1)

    class Bad:
        def __init__(self, path):
            raise KeyError("weird")

    monkeypatch.setattr(detectors, "MediapipePose", P)
    monkeypatch.setattr(detectors, "MediapipeObjects", Bad)
    assert camera_cli._load_models() == cli.EXIT_ERROR
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "Cannot load the camera models" in err[0] and "Traceback" not in err[0]
    assert closed == [1]


def test_load_models_missing_files(monkeypatch, tmp_path, capsys):
    from ha_edge.camera import models

    monkeypatch.setattr(models, "default_model_dir", lambda: tmp_path)
    assert camera_cli._load_models() == cli.EXIT_ERROR
    assert "ha-camera fetch-model" in capsys.readouterr().err


def test_check_open_failure_other_than_busy_is_one_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        "ha_edge.camera.devices.list_cameras", lambda: [CameraInfo(0, "Cam", "/dev/video0")]
    )
    pose, objs = Pose(UP), Objs()

    def boom(i):
        raise OSError("v4l2 exploded")

    monkeypatch.setattr(camera_cli, "_open_camera", boom)
    monkeypatch.setattr(camera_cli, "_load_models", lambda: (pose, objs))
    assert camera_cli.main(["check"]) == cli.EXIT_ERROR
    assert "v4l2 exploded" in capsys.readouterr().err
    assert pose.closed and objs.closed


import pytest


@pytest.mark.camera
def test_load_models_real(monkeypatch, tmp_path):
    import os
    from pathlib import Path

    pytest.importorskip("mediapipe", reason="needs the camera extra")
    pytest.importorskip("cv2")
    from ha_edge.camera import models

    env = os.environ.get("HA_CAMERA_TEST_MODELS")
    if env:
        d = tmp_path / "models"
        d.mkdir()
        (d / models.POSE_FILE).symlink_to(Path(env) / "pose.task")
        (d / models.OBJECT_FILE).symlink_to(Path(env) / "det.tflite")
        monkeypatch.setattr(models, "default_model_dir", lambda: d)
    elif not (models.default_model_dir() / models.POSE_FILE).exists():
        pytest.skip("models not present (run `ha-camera fetch-model`)")
    loaded = camera_cli._load_models()
    assert not isinstance(loaded, int)
    pose, objs = loaded
    try:
        frame = np.zeros((360, 640, 3), np.uint8)
        assert pose.torso(frame) is None
        assert objs.detect(frame) == [] or True
    finally:
        pose.close()
        objs.close()
