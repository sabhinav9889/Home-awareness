import json
import os
import stat

import pytest

from ha_edge import config
from ha_edge.config import AgentConfig, ConfigError, SensorConfig


@pytest.fixture(autouse=True)
def cfg_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HA_EDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


def test_save_then_load_roundtrip():
    s = SensorConfig("http://x", 3, "key", "Mic")
    a = AgentConfig("http://x", "a@b.c", "tok")
    config.save_sensor(s)
    config.save_agent(a)
    assert config.load_sensor() == s
    assert config.load_agent() == a
    assert config.save_sensor(SensorConfig("http://x", 1, "k")).name == "sensor.json"
    assert config.load_sensor().mic_name is None


def test_saved_file_mode_is_0600(cfg_dir):
    p = config.save_sensor(SensorConfig("http://x", 3, "key"))
    q = config.save_agent(AgentConfig("http://x", "a@b.c", "tok"))
    for f in (p, q):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert sorted(x.name for x in cfg_dir.iterdir()) == ["agent.json", "sensor.json"]


def test_missing_file_returns_none():
    assert config.load_sensor() is None
    assert config.load_agent() is None


def test_corrupt_file_raises_clear_error(cfg_dir):
    cfg_dir.mkdir()
    p = cfg_dir / "sensor.json"
    p.write_text("{not json")
    with pytest.raises(ConfigError, match="sensor.json"):
        config.load_sensor()
    p.write_text(json.dumps({"base_url": "x"}))
    with pytest.raises(ConfigError, match="sensor.json"):
        config.load_sensor()


def test_config_dir_default_uses_platformdirs(monkeypatch):
    monkeypatch.delenv("HA_EDGE_CONFIG_DIR")
    assert config.config_dir().name == "home-awareness"


def test_new_config_dir_is_private(cfg_dir):
    config.save_agent(AgentConfig("http://x", "a@b.c", "tok"))
    assert stat.S_IMODE(os.stat(cfg_dir).st_mode) == 0o700
