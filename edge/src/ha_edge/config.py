"""Sensor and agent config storage (JSON files, mode 0600, written atomically)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from platformdirs import user_config_dir


class ConfigError(Exception):
    """A config file exists but cannot be used."""


@dataclass
class SensorConfig:
    base_url: str
    device_id: int
    device_key: str
    mic_name: str | None = None


@dataclass
class CameraConfig:
    base_url: str
    device_id: int
    device_key: str
    camera_name: str | None = None
    camera_index: int | None = None


@dataclass
class AgentConfig:
    base_url: str
    email: str
    # Legacy: a 24 h session JWT, as saved before agent tokens. Still honoured if present.
    access_token: str | None = None
    member_id: int | None = None  # from GET /auth/me; absent in configs saved before it
    agent_token: str | None = None  # long-lived, revocable `ha_agent_…` token
    agent_token_id: int | None = None  # the server's id for it: lets a re-login revoke it

    @property
    def bearer_token(self) -> str | None:
        """What `ha-agent` authenticates with: the agent token, else a legacy JWT."""
        return self.agent_token or self.access_token


def config_dir() -> Path:
    override = os.environ.get("HA_EDGE_CONFIG_DIR")
    return Path(override) if override else Path(user_config_dir("home-awareness"))


def _save(name: str, cfg, *, omit_none: tuple[str, ...] = ()) -> Path:
    data = {k: v for k, v in asdict(cfg).items() if not (k in omit_none and v is None)}
    d = config_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = d / name
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def _load(name: str, cls):
    path = config_dir() / name
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}") from e
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("expected a JSON object")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
    except (ValueError, TypeError) as e:
        raise ConfigError(f"corrupt config file {path}: {e}") from e


def save_sensor(cfg: SensorConfig) -> Path:
    return _save("sensor.json", cfg)


def load_sensor() -> SensorConfig | None:
    return _load("sensor.json", SensorConfig)


def save_agent(cfg: AgentConfig) -> Path:
    # Store only the token the agent has: no `"access_token": null` beside an agent token.
    return _save("agent.json", cfg, omit_none=("access_token", "agent_token", "agent_token_id"))


def load_agent() -> AgentConfig | None:
    return _load("agent.json", AgentConfig)


def save_camera(cfg: CameraConfig) -> Path:
    return _save("camera.json", cfg)


def load_camera() -> CameraConfig | None:
    return _load("camera.json", CameraConfig)
