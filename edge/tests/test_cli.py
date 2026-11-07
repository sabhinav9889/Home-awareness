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


AGENT_TOKEN = "ha_agent_long-lived-secret-abc"


def _mock_agent_token(status=201, json_body=None):
    body = {"id": 9, "token": AGENT_TOKEN} if json_body is None else json_body
    return respx.post("http://srv/me/agent-tokens").mock(
        return_value=httpx.Response(status, json=body)
    )


@respx.mock
def test_agent_login_stores_agent_token_not_password_or_jwt(cfgdir, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "kitchen-laptop")
    route = respx.post("http://srv/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": JWT, "token_type": "bearer"})
    )
    mint = _mock_agent_token()
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
    assert json.loads(mint.calls[0].request.content) == {"name": "kitchen-laptop agent"}
    assert mint.calls[0].request.headers["authorization"] == f"Bearer {JWT}"
    path = cfgdir / "agent.json"
    raw = path.read_text()
    assert PASSWORD not in raw and JWT not in raw
    assert json.loads(raw) == {
        "base_url": "http://srv",
        "email": "me@example.com",
        "member_id": 41,
        "agent_token": AGENT_TOKEN,
        "agent_token_id": 9,
    }
    assert me.calls[0].request.headers["authorization"] == f"Bearer {JWT}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    out = capsys.readouterr()
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (JWT, PASSWORD, AGENT_TOKEN):
        assert secret not in out.out + out.err + logs
    assert "me@example.com" in out.out


@pytest.mark.parametrize(
    ("status", "body", "rc", "msg"),
    [
        (409, {"detail": "too many"}, 2, "Revoke one"),
        (429, {}, 2, "Too many"),
        (500, {}, 1, "HTTP 500"),
        (201, {"id": 9}, 1, "unexpected"),
    ],
)
@respx.mock
def test_agent_login_cannot_create_agent_token(cfgdir, monkeypatch, capsys, status, body, rc, msg):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    respx.post("http://srv/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": JWT, "token_type": "bearer"})
    )
    _mock_agent_token(status, body)
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == rc
    out = capsys.readouterr()
    assert msg in out.err
    assert JWT not in out.out + out.err
    assert not (cfgdir / "agent.json").exists()


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

    config.save_agent(config.AgentConfig("http://srv", "a@b.c", agent_token=AGENT_TOKEN))
    seen = {}

    async def fake_run_agent(cfg, **kw):
        seen["cfg"] = cfg
        seen["kw"] = kw
        return agent.EXIT_RELOGIN

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    assert cli.agent_main(["run"]) == 6
    assert seen["cfg"].agent_token == AGENT_TOKEN
    assert {"connect", "notifier", "alarm", "clock", "http", "stop"} <= set(seen["kw"])
    out = capsys.readouterr()
    assert AGENT_TOKEN not in out.out + out.err
    assert "ha-agent login" in out.err


def test_agent_run_with_agent_token_has_no_expiry_warning_or_hint(
    cfgdir, monkeypatch, capsys, caplog
):
    from ha_edge import agent

    config.save_agent(config.AgentConfig("http://srv", "a@b.c", agent_token=AGENT_TOKEN))

    async def fake_run_agent(cfg, **kw):
        return 0

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    caplog.set_level(logging.DEBUG)
    assert cli.agent_main(["run"]) == 0
    out = capsys.readouterr()
    assert "expire" not in caplog.text and "daily" not in out.out + out.err
    assert AGENT_TOKEN not in out.out + out.err + caplog.text


def test_agent_run_with_legacy_jwt_still_runs_and_hints_to_relogin(cfgdir, monkeypatch, capsys):
    from ha_edge import agent

    (cfgdir / "agent.json").write_text(
        json.dumps({"base_url": "http://srv", "email": "a@b.c", "access_token": JWT})
    )
    seen = {}

    async def fake_run_agent(cfg, **kw):
        seen["cfg"] = cfg
        return 0

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    assert cli.agent_main(["run"]) == 0
    assert seen["cfg"].bearer_token == JWT
    out = capsys.readouterr()
    assert "re-run `ha-agent login` to stop daily logins" in out.err
    assert JWT not in out.out + out.err


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
    _mock_agent_token()
    respx.get("http://srv/auth/me").mock(return_value=httpx.Response(500))
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 0
    saved = config.load_agent()
    assert saved.agent_token == AGENT_TOKEN and saved.member_id is None
    assert saved.access_token is None


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


def test_agent_run_warns_when_the_stored_login_is_about_to_expire(cfgdir, monkeypatch, caplog):
    import base64
    import time

    from ha_edge import agent

    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'HS256'})}.{b64({'sub': '1', 'exp': int(time.time()) + 3600})}.sig"
    config.save_agent(config.AgentConfig("http://srv", "a@b.c", token))

    async def fake_run_agent(cfg, **kw):
        return 0

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    caplog.set_level(logging.INFO)
    assert cli.agent_main(["run"]) == 0
    assert "less than 2 h" in caplog.text
    assert token not in caplog.text


def _relogin_mocks():
    respx.post("http://srv/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": JWT, "token_type": "bearer"})
    )
    _mock_agent_token(json_body={"id": 12, "token": AGENT_TOKEN})
    respx.get("http://srv/auth/me").mock(return_value=httpx.Response(200, json={"id": 41}))


@respx.mock
def test_agent_relogin_revokes_the_previous_token(cfgdir, monkeypatch, capsys):
    from ha_edge import config

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    config.save_agent(
        config.AgentConfig("http://srv", "a@b.c", agent_token="ha_agent_old", agent_token_id=5)
    )
    _relogin_mocks()
    gone = respx.delete("http://srv/me/agent-tokens/5").mock(return_value=httpx.Response(204))
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 0
    assert gone.calls[0].request.headers["authorization"] == f"Bearer {JWT}"
    saved = json.loads((cfgdir / "agent.json").read_text())
    assert saved["agent_token"] == AGENT_TOKEN and saved["agent_token_id"] == 12
    out = capsys.readouterr()
    for secret in (JWT, PASSWORD, AGENT_TOKEN, "ha_agent_old"):
        assert secret not in out.out + out.err


@respx.mock
@pytest.mark.parametrize(
    "outcome", [httpx.Response(404), httpx.Response(500), httpx.ConnectError("refused")]
)
def test_agent_relogin_survives_a_failing_revoke(cfgdir, monkeypatch, capsys, outcome):
    from ha_edge import config

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    config.save_agent(
        config.AgentConfig("http://srv", "a@b.c", agent_token="ha_agent_old", agent_token_id=5)
    )
    _relogin_mocks()
    route = respx.delete("http://srv/me/agent-tokens/5")
    if isinstance(outcome, Exception):
        route.mock(side_effect=outcome)
    else:
        route.mock(return_value=outcome)
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 0
    assert route.called
    assert json.loads((cfgdir / "agent.json").read_text())["agent_token"] == AGENT_TOKEN
    assert "Logged in" in capsys.readouterr().out


@respx.mock
def test_agent_login_without_an_old_id_revokes_nothing(cfgdir, monkeypatch):
    from ha_edge import config

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    config.save_agent(config.AgentConfig("http://srv", "a@b.c", access_token="legacy.jwt"))
    _relogin_mocks()  # any DELETE would raise: respx has no route for it
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 0


def test_config_without_agent_token_id_still_loads(cfgdir):
    (cfgdir / "agent.json").write_text(
        json.dumps({"base_url": "http://srv", "email": "a@b.c", "agent_token": "ha_agent_x"})
    )
    from ha_edge import config

    assert config.load_agent().agent_token_id is None


@respx.mock
def test_agent_relogin_to_another_server_revokes_nothing(cfgdir, monkeypatch):
    from ha_edge import config

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    config.save_agent(
        config.AgentConfig("http://other", "a@b.c", agent_token="ha_agent_old", agent_token_id=5)
    )
    _relogin_mocks()
    other = respx.delete(url__regex=r"http://.*/me/agent-tokens/5").mock(
        return_value=httpx.Response(204)
    )
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 0
    assert not other.called


# --- M2: save first, then revoke ------------------------------------------------------


@respx.mock
def test_agent_relogin_saves_the_new_token_before_revoking_the_old(cfgdir, monkeypatch):
    from ha_edge import config

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    config.save_agent(
        config.AgentConfig("http://srv", "a@b.c", agent_token="ha_agent_old", agent_token_id=5)
    )
    _relogin_mocks()
    seen = []

    def at_revoke(request):
        seen.append(json.loads((cfgdir / "agent.json").read_text())["agent_token"])
        return httpx.Response(204)

    respx.delete("http://srv/me/agent-tokens/5").mock(side_effect=at_revoke)
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 0
    assert seen == [AGENT_TOKEN]  # the new token was on disk when the old one was revoked


@respx.mock
@pytest.mark.parametrize("revoke_new", [httpx.Response(204), httpx.ConnectError("refused")])
def test_agent_relogin_save_failure_revokes_the_new_token_and_keeps_the_old(
    cfgdir, monkeypatch, capsys, revoke_new
):
    from ha_edge import config

    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    config.save_agent(
        config.AgentConfig("http://srv", "a@b.c", agent_token="ha_agent_old", agent_token_id=5)
    )
    before = (cfgdir / "agent.json").read_text()
    _relogin_mocks()
    old = respx.delete("http://srv/me/agent-tokens/5").mock(return_value=httpx.Response(204))
    new = respx.delete("http://srv/me/agent-tokens/12")
    if isinstance(revoke_new, Exception):
        new.mock(side_effect=revoke_new)
    else:
        new.mock(return_value=revoke_new)

    def disk_full(cfg):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(config, "save_agent", disk_full)
    assert cli.agent_main(["login", "--server", "http://srv", "--email", "a@b.c"]) == 1
    assert new.called  # no orphaned live token on the server
    assert new.calls[0].request.headers["authorization"] == f"Bearer {JWT}"
    assert not old.called  # the stored login still works
    assert (cfgdir / "agent.json").read_text() == before
    out = capsys.readouterr()
    assert "cannot save" in out.err.lower() and "No space left on device" in out.err
    assert "Logged in" not in out.out
    for secret in (JWT, PASSWORD, AGENT_TOKEN, "ha_agent_old"):
        assert secret not in out.out + out.err


# --- check -----------------------------------------------------------------------------


@pytest.fixture
def check_env(cfgdir, monkeypatch):
    from ha_edge import audio, check, model

    calls = {}
    monkeypatch.setattr(audio, "list_inputs", lambda: DEVICES)
    monkeypatch.setattr(model, "fetch_model", lambda *a, **k: "p")

    class FakeClf:
        def __init__(self, path):
            self.class_names = []

    monkeypatch.setattr(model, "YamnetClassifier", FakeClf)

    async def fake_check(classifier, mic, *, seconds, thresholds, enabled=None, out=print):
        calls.update(mic=mic, seconds=seconds, thresholds=thresholds, enabled=enabled)
        out("level 0.100 peak 0.10 | Speech 0.90 | -")
        return calls.get("result")

    monkeypatch.setattr(check, "run_check", fake_check)
    return calls


def test_check_unpaired_uses_default_mic_and_default_thresholds(check_env, capsys):
    assert cli.sensor_main(["check", "--seconds", "5"]) == 0
    assert check_env["mic"].device.name == "alsa_input.pci.analog"
    assert check_env["seconds"] == 5
    assert check_env["thresholds"] == {} and check_env["enabled"] is None
    out = capsys.readouterr().out
    assert "Not paired: using the default threshold (0.30)" in out
    assert "Built-in Audio Analog Stereo" in out and "Nothing is recorded or sent" in out
    assert "level 0.100" in out


@respx.mock
def test_check_paired_uses_pinned_mic_and_server_thresholds_without_pinning(check_env, capsys):
    config.save_sensor(config.SensorConfig("http://srv", 42, KEY, "alsa_input.usb.headset"))
    route = respx.get("http://srv/device/config").respond(
        200,
        json={
            "enabled_sounds": ["doorbell"],
            "heartbeat_interval_s": 60,
            "thresholds": {"doorbell": 0.4},
        },
    )
    assert cli.sensor_main(["check"]) == 0
    assert route.called
    assert check_env["mic"].device.name == "alsa_input.usb.headset"
    assert check_env["seconds"] == 15  # default
    assert check_env["thresholds"] == {"doorbell": 0.4}
    assert check_env["enabled"] == frozenset({"doorbell"})
    out = capsys.readouterr()
    assert "Using your household's thresholds from http://srv" in out.out
    assert KEY not in out.out + out.err


@respx.mock
def test_check_mic_flag_does_not_change_the_pin(check_env):
    config.save_sensor(config.SensorConfig("http://srv", 42, KEY, "alsa_input.pci.analog"))
    respx.get("http://srv/device/config").respond(503)
    assert cli.sensor_main(["check", "--mic", "usb"]) == 0
    assert check_env["mic"].device.name == "alsa_input.usb.headset"
    assert config.load_sensor().mic_name == "alsa_input.pci.analog"


@respx.mock
def test_check_server_unreachable_falls_back_to_defaults(check_env, capsys):
    config.save_sensor(config.SensorConfig("http://srv", 42, KEY))
    respx.get("http://srv/device/config").mock(side_effect=httpx.ConnectError("x"))
    assert cli.sensor_main(["check"]) == 0
    assert check_env["thresholds"] == {} and check_env["enabled"] is None
    assert "using the default threshold (0.30)" in capsys.readouterr().out


def test_check_missing_pinned_mic_exits_4(check_env, capsys):
    config.save_sensor(config.SensorConfig("http://srv", 42, KEY, "alsa_input.gone"))
    assert cli.sensor_main(["check"]) == 4
    assert "mic" not in check_env
    assert "alsa_input.gone" in capsys.readouterr().err


def test_check_mic_lost_exits_4(check_env, monkeypatch, capsys):
    from ha_edge import audio, check

    async def lost(*a, **k):
        raise audio.MicLost("unplugged")

    monkeypatch.setattr(check, "run_check", lost)
    assert cli.sensor_main(["check"]) == 4
    assert "Microphone lost" in capsys.readouterr().err


def test_check_rejects_non_positive_seconds(check_env):
    with pytest.raises(SystemExit) as e:
        cli.sensor_main(["check", "--seconds", "0"])
    assert e.value.code == 2
