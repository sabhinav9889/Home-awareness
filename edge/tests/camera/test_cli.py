import hashlib
import json
import logging
import stat

import httpx
import pytest
import respx

from ha_edge import cli, config
from ha_edge.camera import cli as camera_cli
from ha_edge.camera import devices, models
from ha_edge.model import ModelIntegrityError

KEY = "very-secret-camera-key-xyz"


@pytest.fixture
def cfgdir(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


def test_camera_config_roundtrip_0600(cfgdir):
    cfg = config.CameraConfig("http://srv", 7, KEY, "Desk cam", 2)
    path = config.save_camera(cfg)
    assert path.name == "camera.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.load_camera() == cfg
    assert config.load_sensor() is None


def test_camera_config_defaults_and_missing(cfgdir):
    assert config.load_camera() is None
    config.save_camera(config.CameraConfig("http://srv", 1, "k"))
    loaded = config.load_camera()
    assert loaded.camera_name is None and loaded.camera_index is None


def test_camera_config_corrupt_raises(cfgdir):
    cfgdir.mkdir(parents=True)
    (cfgdir / "camera.json").write_text("[1]")
    with pytest.raises(config.ConfigError):
        config.load_camera()


def _fake_sysfs(root, nodes):
    for node, (name, index) in nodes.items():
        d = root / node
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        (d / "index").write_text(f"{index}\n")


def test_list_cameras_skips_metadata_nodes(tmp_path):
    _fake_sysfs(
        tmp_path,
        {
            "video0": ("Integrated Camera: Integrated C", 0),
            "video1": ("Integrated Camera: Integrated C", 1),
            "video2": ("USB Cam", 0),
            "video10": ("Other", 0),
        },
    )
    cams = devices.list_cameras(tmp_path)
    assert [(c.index, c.name, c.node) for c in cams] == [
        (0, "Integrated Camera: Integrated C", "/dev/video0"),
        (2, "USB Cam", "/dev/video2"),
        (10, "Other", "/dev/video10"),
    ]


def test_list_cameras_missing_dir_and_bad_entries(tmp_path):
    assert devices.list_cameras(tmp_path / "nope") == []
    (tmp_path / "video3").mkdir()  # no name/index files
    (tmp_path / "notvideo").mkdir()
    assert devices.list_cameras(tmp_path) == []


def test_cameras_command_prints_list(monkeypatch, capsys):
    monkeypatch.setattr(
        devices, "list_cameras", lambda *a: [devices.CameraInfo(0, "Webcam", "/dev/video0")]
    )
    assert camera_cli.main(["cameras"]) == 0
    out = capsys.readouterr().out
    assert "Webcam" in out and "/dev/video0" in out


def test_cameras_command_none_found(monkeypatch, capsys):
    monkeypatch.setattr(devices, "list_cameras", lambda *a: [])
    assert camera_cli.main(["cameras"]) == 0
    assert "No cameras found" in capsys.readouterr().out


@respx.mock
def test_pair_saves_camera_json_and_never_prints_key(cfgdir, capsys, caplog, monkeypatch):
    monkeypatch.setattr(devices, "list_cameras", list)
    caplog.set_level(logging.DEBUG)
    respx.post("http://srv/device/pair").mock(
        return_value=httpx.Response(201, json={"device_id": 9, "device_key": KEY})
    )
    argv = ["pair", "--server", "http://srv", "--code", "ABC123", "--camera", "Hall"]
    assert camera_cli.main(argv) == 0
    out = capsys.readouterr()
    assert "Paired as device 9." in out.out
    assert "snapshots only on alerts" in out.out
    assert KEY not in out.out + out.err + caplog.text
    path = cfgdir / "camera.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["device_key"] == KEY and data["device_id"] == 9
    assert data["camera_name"] == "Hall"
    assert not (cfgdir / "sensor.json").exists()


@respx.mock
def test_pair_keeps_previous_camera_choice_without_flag(cfgdir):
    config.save_camera(config.CameraConfig("http://srv", 1, "old", "Desk", 2))
    respx.post("http://srv/device/pair").mock(
        return_value=httpx.Response(201, json={"device_id": 2, "device_key": KEY})
    )
    assert camera_cli.main(["pair", "--server", "http://srv", "--code", "ABC123"]) == 0
    cfg = config.load_camera()
    assert (cfg.device_id, cfg.camera_name, cfg.camera_index) == (2, "Desk", 2)


@respx.mock
def test_pair_bad_code_exit_2_saves_nothing(cfgdir, capsys):
    respx.post("http://srv/device/pair").mock(
        return_value=httpx.Response(400, json={"detail": "invalid or expired pairing code"})
    )
    assert camera_cli.main(["pair", "--server", "http://srv", "--code", "BAD"]) == 2
    assert "Pairing failed" in capsys.readouterr().err
    assert not (cfgdir / "camera.json").exists()


def test_help_has_disclaimer_and_subcommands(capsys):
    with pytest.raises(SystemExit) as e:
        camera_cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert cli.DISCLAIMER in out
    for word in ("pair", "cameras", "fetch-model"):
        assert word in out


def test_ha_sensor_help_unchanged(capsys):
    with pytest.raises(SystemExit) as e:
        cli.sensor_main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "camera" not in out.lower()


POSE = b"pose-bytes" * 50
OBJ = b"object-bytes" * 50


@pytest.fixture
def patched_hashes(monkeypatch):
    monkeypatch.setattr(models, "POSE_SHA256", hashlib.sha256(POSE).hexdigest())
    monkeypatch.setattr(models, "OBJECT_SHA256", hashlib.sha256(OBJ).hexdigest())


def test_pinned_hashes_are_full_sha256():
    for h in (models.POSE_SHA256, models.OBJECT_SHA256):
        assert len(h) == 64 and int(h, 16) >= 0
    assert models.POSE_FILE == "pose_landmarker_lite.task"
    assert models.OBJECT_FILE == "efficientdet_lite0.tflite"


@respx.mock
def test_fetch_models_good_hash(tmp_path, patched_hashes):
    respx.get(models.POSE_URL).mock(return_value=httpx.Response(200, content=POSE))
    respx.get(models.OBJECT_URL).mock(return_value=httpx.Response(200, content=OBJ))
    d = models.fetch_models(tmp_path)
    assert d == tmp_path
    assert (tmp_path / models.POSE_FILE).read_bytes() == POSE
    assert (tmp_path / models.OBJECT_FILE).read_bytes() == OBJ
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [models.POSE_FILE, models.OBJECT_FILE]
    )


@respx.mock
def test_fetch_models_bad_hash_raises_and_leaves_no_file(tmp_path, patched_hashes):
    respx.get(models.POSE_URL).mock(return_value=httpx.Response(200, content=POSE))
    respx.get(models.OBJECT_URL).mock(return_value=httpx.Response(200, content=b"tampered"))
    with pytest.raises(ModelIntegrityError):
        models.fetch_models(tmp_path)
    assert not (tmp_path / models.OBJECT_FILE).exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]


@respx.mock
def test_fetch_models_noop_when_present(tmp_path, patched_hashes):
    (tmp_path / models.POSE_FILE).write_bytes(POSE)
    (tmp_path / models.OBJECT_FILE).write_bytes(OBJ)
    p = respx.get(models.POSE_URL).mock(return_value=httpx.Response(500))
    o = respx.get(models.OBJECT_URL).mock(return_value=httpx.Response(500))
    assert models.fetch_models(tmp_path) == tmp_path
    assert not p.called and not o.called


@respx.mock
def test_fetch_model_command(monkeypatch, tmp_path, capsys, patched_hashes):
    monkeypatch.setattr(models, "default_model_dir", lambda: tmp_path)
    respx.get(models.POSE_URL).mock(return_value=httpx.Response(200, content=POSE))
    respx.get(models.OBJECT_URL).mock(return_value=httpx.Response(200, content=OBJ))
    assert camera_cli.main(["fetch-model"]) == 0
    out = capsys.readouterr().out
    assert models.POSE_FILE in out and models.OBJECT_FILE in out


@respx.mock
def test_fetch_model_command_integrity_failure_exit_1(
    monkeypatch, tmp_path, capsys, patched_hashes
):
    monkeypatch.setattr(models, "default_model_dir", lambda: tmp_path)
    respx.get(models.POSE_URL).mock(return_value=httpx.Response(200, content=b"bad"))
    assert camera_cli.main(["fetch-model"]) == 1
    assert "Cannot download" in capsys.readouterr().err


CAMS = [
    devices.CameraInfo(0, "Integrated Camera: Integrated C", "/dev/video0"),
    devices.CameraInfo(2, "Logitech BRIO", "/dev/video2"),
]


def _pair(monkeypatch, cams, *flag):
    monkeypatch.setattr(devices, "list_cameras", lambda: cams)
    route = respx.post("http://srv/device/pair").mock(
        return_value=httpx.Response(201, json={"device_id": 9, "device_key": KEY})
    )
    argv = ["pair", "--server", "http://srv", "--code", "ABC123", *flag]
    return camera_cli.main(argv), route


@respx.mock
def test_pair_saves_the_resolved_full_name_and_index(cfgdir, monkeypatch):
    code, _ = _pair(monkeypatch, CAMS, "--camera", "Integrated")  # the README example
    assert code == 0
    cfg = config.load_camera()
    assert (cfg.camera_name, cfg.camera_index) == ("Integrated Camera: Integrated C", 0)
    # and run/check can then find it by the saved name
    assert camera_cli._pick_camera(None, cfg) == CAMS[0]


@respx.mock
def test_pair_substring_is_case_insensitive_and_exact_wins(cfgdir, monkeypatch):
    code, _ = _pair(monkeypatch, CAMS, "--camera", "brio")
    assert code == 0 and config.load_camera().camera_name == "Logitech BRIO"
    cams = [*CAMS, devices.CameraInfo(4, "Logitech BRIO 4K", "/dev/video4")]
    code, _ = _pair(monkeypatch, cams, "--camera", "Logitech BRIO")
    assert code == 0 and config.load_camera().camera_index == 2


@respx.mock
def test_pair_ambiguous_name_exits_1_lists_cameras_saves_nothing(cfgdir, monkeypatch, capsys):
    code, route = _pair(monkeypatch, CAMS, "--camera", "i")
    err = capsys.readouterr().err
    assert code == 1 and "more than one" in err and "Logitech BRIO" in err
    assert not (cfgdir / "camera.json").exists() and not route.called


@respx.mock
def test_pair_unknown_name_exits_1_lists_cameras_saves_nothing(cfgdir, monkeypatch, capsys):
    code, route = _pair(monkeypatch, CAMS, "--camera", "nope")
    err = capsys.readouterr().err
    assert code == 1 and "not found" in err and "Integrated Camera" in err
    assert not (cfgdir / "camera.json").exists() and not route.called


@respx.mock
def test_pair_without_any_cameras_saves_name_as_typed(cfgdir, monkeypatch):
    code, _ = _pair(monkeypatch, [], "--camera", "Integrated")
    assert code == 0
    cfg = config.load_camera()
    assert (cfg.camera_name, cfg.camera_index) == ("Integrated", None)
