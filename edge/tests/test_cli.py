import json
import logging
import stat

import httpx
import pytest
import respx

from ha_edge import cli, config
from ha_edge.audio import InputDevice

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."


@pytest.mark.parametrize("entry", ["sensor_main", "agent_main"])
def test_help_contains_disclaimer(entry, capsys):
    with pytest.raises(SystemExit) as e:
        getattr(cli, entry)(["--help"])
    assert e.value.code == 0
    assert DISCLAIMER in capsys.readouterr().out


def test_fetch_model_prints_path(monkeypatch, capsys, tmp_path):
    from ha_edge import model

    target = tmp_path / "yamnet.tflite"
    monkeypatch.setattr(model, "fetch_model", lambda d, **kw: target)
    assert cli.sensor_main(["fetch-model"]) == 0
    assert capsys.readouterr().out.strip() == str(target)


# --- pair / devices / run -------------------------------------------------------------

KEY = "very-secret-device-key-xyz"


@pytest.fixture
def cfgdir(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path))
    return tmp_path


@respx.mock
def test_pair_saves_config_0600_and_never_prints_key(cfgdir, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    respx.post("http://srv/device/pair").mock(
        return_value=httpx.Response(201, json={"device_id": 42, "device_key": KEY})
    )
    assert cli.sensor_main(["pair", "--server", "http://srv", "--code", "ABC123"]) == 0
    out = capsys.readouterr()
    assert "Paired as device 42." in out.out
    assert KEY not in out.out + out.err + caplog.text
    path = cfgdir / "sensor.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["device_key"] == KEY and data["device_id"] == 42
    assert data["base_url"] == "http://srv"


@respx.mock
def test_pair_failure_is_friendly_exit_2(cfgdir, capsys):
    respx.post("http://srv/device/pair").mock(
        return_value=httpx.Response(400, json={"detail": "invalid or expired code"})
    )
    assert cli.sensor_main(["pair", "--server", "http://srv", "--code", "BAD"]) == 2
    err = capsys.readouterr().err
    assert "Pairing failed" in err and "Traceback" not in err
    assert not (cfgdir / "sensor.json").exists()


DEVICES = [
    InputDevice(51, "alsa_input.pci.analog", 48000.0, True, "Built-in Audio Analog Stereo"),
    InputDevice(77, "alsa_input.usb.headset", 48000.0, False, "USB Headset Mic"),
]


def test_devices_lists_inputs_with_default_marker(monkeypatch, capsys):
    from ha_edge import audio

    monkeypatch.setattr(audio, "list_inputs", lambda: DEVICES)
    assert cli.sensor_main(["devices"]) == 0
    lines = capsys.readouterr().out.splitlines()
    row = next(line for line in lines if "alsa_input.pci.analog" in line)
    assert "51" in row and "Built-in Audio Analog Stereo" in row and "*" in row
    row2 = next(line for line in lines if "alsa_input.usb.headset" in line)
    assert "*" not in row2


def test_usage_error_exits_2(capsys):
    with pytest.raises(SystemExit) as e:
        cli.sensor_main(["pair", "--server", "http://srv"])
    assert e.value.code == 2


def test_run_not_paired_exits_3(cfgdir, capsys):
    assert cli.sensor_main(["run"]) == 3
    assert "ha-sensor pair" in capsys.readouterr().err


@pytest.fixture
def run_env(cfgdir, monkeypatch):
    from ha_edge import audio, model, sensor

    calls = {}
    monkeypatch.setattr(audio, "list_inputs", lambda: DEVICES)
    monkeypatch.setattr(model, "fetch_model", lambda *a, **k: calls.setdefault("fetched", "p"))

    class FakeClf:
        def __init__(self, path):
            calls["clf_path"] = path
            self.class_names = []

    monkeypatch.setattr(model, "YamnetClassifier", FakeClf)

    async def fake_run(cfg, **kw):
        calls["mic"] = kw["mic"]
        calls["cfg"] = cfg
        return calls.get("code", 0)

    monkeypatch.setattr(sensor, "run_sensor", fake_run)
    config.save_sensor(config.SensorConfig("http://srv", 42, KEY))
    return calls


def test_run_pins_default_mic_on_first_start(run_env, capsys):
    assert cli.sensor_main(["run"]) == 0
    assert config.load_sensor().mic_name == "alsa_input.pci.analog"
    assert run_env["mic"].device.name == "alsa_input.pci.analog"
    assert run_env["clf_path"] == "p"  # model obtained via fetch_model (integrity check)
    out = capsys.readouterr()
    assert out.out.count("Built-in Audio Analog Stereo") == 1
    assert KEY not in out.out + out.err


def test_run_mic_flag_resolves_and_pins(run_env, capsys):
    assert cli.sensor_main(["run", "--mic", "usb"]) == 0
    assert config.load_sensor().mic_name == "alsa_input.usb.headset"
    assert run_env["mic"].device.name == "alsa_input.usb.headset"


def test_run_missing_pinned_mic_exits_4_without_fallback(run_env, capsys):
    cfg = config.load_sensor()
    cfg.mic_name = "alsa_input.gone"
    config.save_sensor(cfg)
    assert cli.sensor_main(["run"]) == 4
    assert "mic" not in run_env
    assert "alsa_input.gone" in capsys.readouterr().err
    assert config.load_sensor().mic_name == "alsa_input.gone"


def test_run_returns_sensor_exit_code(run_env):
    run_env["code"] = 5
    assert cli.sensor_main(["run"]) == 5


def test_pinned_mic_matches_exactly_not_by_substring(run_env, capsys):
    cfg = config.load_sensor()
    cfg.mic_name = "alsa_input.usb"  # substring of the headset's name, but not its name
    config.save_sensor(cfg)
    assert cli.sensor_main(["run"]) == 4
    assert "mic" not in run_env


def test_sensor_and_cli_exit_codes_agree():
    from ha_edge import sensor

    assert (sensor.EXIT_OK, sensor.EXIT_MIC_LOST, sensor.EXIT_REVOKED) == (
        cli.EXIT_OK,
        cli.EXIT_MIC_LOST,
        cli.EXIT_REVOKED,
    )
