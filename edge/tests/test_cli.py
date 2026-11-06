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


def test_ctrl_c_during_setup_exits_quietly(run_env, monkeypatch, capsys):
    from ha_edge import model

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(model, "fetch_model", interrupted)
    assert cli.sensor_main(["run"]) == 0
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("no network"), OSError("disk full")],
)
def test_fetch_model_errors_are_friendly_exit_1(monkeypatch, capsys, exc):
    from ha_edge import model

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(model, "fetch_model", boom)
    assert cli.sensor_main(["fetch-model"]) == 1
    assert "Cannot download the sound model" in capsys.readouterr().err


def test_fetch_model_integrity_error_exit_1(monkeypatch, capsys):
    from ha_edge import model

    def boom(*a, **k):
        raise model.ModelIntegrityError("sha mismatch")

    monkeypatch.setattr(model, "fetch_model", boom)
    assert cli.sensor_main(["fetch-model"]) == 1
    assert "sha mismatch" in capsys.readouterr().err


def test_run_mic_listing_failure_exits_1(run_env, monkeypatch, capsys):
    from ha_edge import audio

    def broken():
        raise RuntimeError("pw-dump failed: not found")

    monkeypatch.setattr(audio, "list_inputs", broken)
    assert cli.sensor_main(["run"]) == 1
    assert "pw-dump" in capsys.readouterr().err


# --- ha-agent ---------------------------------------------------------------------------

PASSWORD = "hunter2-very-secret"
JWT = "header.payload.sig-agent-token"


@respx.mock
def test_agent_login_stores_token_not_password(cfgdir, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    route = respx.post("http://srv/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": JWT, "token_type": "bearer"})
    )
    me = respx.get("http://srv/auth/me").mock(
        return_value=httpx.Response(
            200, json={"id": 41, "email": "me@example.com", "role": "owner", "household_id": 2}
        )
    )
    rc = cli.agent_main(["login", "--server", "http://srv/", "--email", "me@example.com"])
    assert rc == 0
    assert json.loads(route.calls[0].request.content) == {
        "email": "me@example.com",
        "password": PASSWORD,
    }
    path = cfgdir / "agent.json"
    raw = path.read_text()
    assert PASSWORD not in raw
    assert json.loads(raw) == {
        "base_url": "http://srv",
        "email": "me@example.com",
        "access_token": JWT,
        "member_id": 41,
    }
    assert me.calls[0].request.headers["authorization"] == f"Bearer {JWT}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    out = capsys.readouterr()
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (JWT, PASSWORD):
        assert secret not in out.out + out.err + logs
    assert "me@example.com" in out.out


@pytest.mark.parametrize(
    ("status", "msg"),
    [(401, "wrong email or password"), (403, "not confirmed"), (429, "Too many")],
)
@respx.mock
def test_agent_login_rejected(cfgdir, monkeypatch, capsys, status, msg):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    respx.post("http://srv/auth/login").mock(return_value=httpx.Response(status, json={}))
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 2
    assert msg in capsys.readouterr().err
    assert not (cfgdir / "agent.json").exists()


@respx.mock
def test_agent_login_unreachable_exit_1(cfgdir, monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    respx.post("http://srv/auth/login").mock(side_effect=httpx.ConnectError("refused"))
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 1
    assert "cannot reach" in capsys.readouterr().err


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_agent_login_ctrl_c_at_password_prompt_exits_quietly(cfgdir, monkeypatch, capsys, exc):
    def interrupted(prompt=""):
        raise exc

    monkeypatch.setattr(cli.getpass, "getpass", interrupted)
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 0
    assert "Traceback" not in capsys.readouterr().err


def test_agent_run_not_logged_in(cfgdir, capsys):
    assert cli.agent_main(["run"]) == 3
    assert "ha-agent login" in capsys.readouterr().err


def test_agent_run_passes_through_relogin_exit(cfgdir, monkeypatch, capsys):
    from ha_edge import agent

    config.save_agent(config.AgentConfig("http://srv", "a@b.c", JWT))
    seen = {}

    async def fake_run_agent(cfg, **kw):
        seen["cfg"] = cfg
        seen["kw"] = kw
        return agent.EXIT_RELOGIN

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    assert cli.agent_main(["run"]) == 6
    assert seen["cfg"].access_token == JWT
    assert {"connect", "notifier", "alarm", "clock", "http", "stop"} <= set(seen["kw"])
    out = capsys.readouterr()
    assert JWT not in out.out + out.err
    assert "ha-agent login" in out.err


def test_agent_test_alarm_plays_3s(monkeypatch):
    from ha_edge import notify

    made = []

    class FakePlayer:
        def __init__(self, max_s=30.0, **kw):
            self.max_s = max_s
            self.played = self.waited = False
            self.unavailable = False
            made.append(self)

        async def play(self):
            self.played = True

        async def wait(self):
            self.waited = True

        def stop(self):
            pass

    monkeypatch.setattr(notify, "AlarmPlayer", FakePlayer)
    assert cli.agent_main(["test-alarm"]) == 0
    assert made[0].max_s == 3.0 and made[0].played and made[0].waited


def test_agent_test_alarm_without_pw_play_exits_1(monkeypatch, capsys):
    from ha_edge import notify

    class Missing:
        def __init__(self, **kw):
            self.unavailable = False

        async def play(self):
            self.unavailable = True

        async def wait(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(notify, "AlarmPlayer", Missing)
    assert cli.agent_main(["test-alarm"]) == 1
    assert "pw-play" in capsys.readouterr().err


@respx.mock
def test_agent_login_without_me_still_saves_token(cfgdir, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    respx.post("http://srv/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": JWT, "token_type": "bearer"})
    )
    respx.get("http://srv/auth/me").mock(return_value=httpx.Response(500))
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 0
    saved = config.load_agent()
    assert saved.access_token == JWT and saved.member_id is None


def test_old_agent_config_without_member_id_loads(cfgdir):
    (cfgdir / "agent.json").write_text(
        json.dumps({"base_url": "http://srv", "email": "a@b.c", "access_token": JWT})
    )
    saved = config.load_agent()
    assert saved.member_id is None and saved.access_token == JWT


# --- final review -----------------------------------------------------------------------


@respx.mock
def test_pair_rate_limited_says_wait(cfgdir, capsys):
    respx.post("http://srv/device/pair").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "42"})
    )
    assert cli.sensor_main(["pair", "--server", "http://srv", "--code", "ABC123"]) == 2
    err = capsys.readouterr().err
    assert "Too many attempts — wait 42 s and try again" in err
    assert "cannot reach" not in err


@respx.mock
def test_pair_404_says_check_the_server_url(cfgdir, capsys):
    respx.post("http://srv/device/pair").mock(
        return_value=httpx.Response(404, json={"detail": "Not Found"})
    )
    assert cli.sensor_main(["pair", "--server", "http://srv", "--code", "ABC123"]) == 2
    err = capsys.readouterr().err
    assert "404" in err and "check the server URL" in err
    assert "cannot reach" not in err
