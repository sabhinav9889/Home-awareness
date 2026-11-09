"""Static checks of the production deployment files (no stack is started)."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ["DOMAIN", "ACME_EMAIL", "POSTGRES_PASSWORD", "JWT_SECRET", "SNAPSHOT_KEY"]
HAVE_DOCKER = shutil.which("docker") is not None
needs_docker = pytest.mark.skipif(not HAVE_DOCKER, reason="docker not available")


def _env(values: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in REQUIRED + ["PUBLIC_BASE_URL"]}
    env.update(values)
    return env


GOOD = {
    "DOMAIN": "localhost",
    "ACME_EMAIL": "ops@example.com",
    "POSTGRES_PASSWORD": "pw-for-tests",
    "JWT_SECRET": "j" * 40,
    "SNAPSHOT_KEY": Fernet.generate_key().decode(),
}


def _config(values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml",
            "-p", "ha-config-only", "config", "--format", "json",
        ],
        cwd=ROOT, env=_env(values), capture_output=True, text=True, timeout=60,
    )  # fmt: skip


@pytest.fixture(scope="module")
def merged() -> dict:
    r = _config(GOOD)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_docker
def test_no_host_ports_except_caddy(merged):
    services = merged["services"]
    for name in ("db", "redis", "api", "worker", "beat", "migrate"):
        assert not services[name].get("ports"), name
    published = {(p["target"], p.get("protocol", "tcp")) for p in services["caddy"]["ports"]}
    assert published == {(80, "tcp"), (443, "tcp"), (443, "udp")}


@needs_docker
def test_database_password_consistent(merged):
    services = merged["services"]
    assert services["db"]["environment"]["POSTGRES_PASSWORD"] == GOOD["POSTGRES_PASSWORD"]
    for name in ("migrate", "api", "worker", "beat"):
        url = services[name]["environment"]["DATABASE_URL"]
        assert f"home:{GOOD['POSTGRES_PASSWORD']}@db:5432/home" in url, name


@needs_docker
def test_proxy_and_restart_settings(merged):
    services = merged["services"]
    assert services["api"]["environment"]["FORWARDED_ALLOW_IPS"] == "*"
    assert services["api"]["environment"]["PUBLIC_BASE_URL"] == "https://localhost"
    for name in ("caddy", "db", "redis", "api", "worker", "beat"):
        assert services[name]["restart"] == "unless-stopped", name
        assert services[name]["logging"]["driver"] == "json-file", name
    assert services["caddy"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert {"caddy_data", "caddy_config"} <= set(merged["volumes"])


@needs_docker
@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_required_variable_fails_with_its_name(missing):
    values = {k: v for k, v in GOOD.items() if k != missing}
    r = _config(values)
    assert r.returncode != 0
    assert missing in r.stderr


@needs_docker
def test_base_compose_still_works_for_local_dev():
    r = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-p", "ha-config-only", "config"],
        cwd=ROOT, env=_env({"JWT_SECRET": "x" * 40}), capture_output=True, text=True,
        timeout=60,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "127.0.0.1" in r.stdout  # dev ports are still published (loopback only)


@needs_docker
def test_caddyfile_validates():
    # Check first: docker would create a root-owned directory for a missing bind-mount source.
    assert (ROOT / "deploy" / "Caddyfile").is_file()
    r = subprocess.run(
        [
            "docker", "run", "--rm", "-e", "DOMAIN=localhost", "-e", "ACME_EMAIL=a@example.com",
            "-v", f"{ROOT / 'deploy' / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
            "caddy:2-alpine", "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
            "--adapter", "caddyfile",
        ],
        capture_output=True, text=True, timeout=180,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr


def test_env_prod_is_gitignored():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    r = subprocess.run(
        ["git", "check-ignore", "-q", "deploy/.env.prod"], cwd=ROOT, capture_output=True
    )
    assert r.returncode == 0


def test_example_documents_every_variable():
    text = (ROOT / "deploy" / ".env.prod.example").read_text()
    for name in [
        *REQUIRED, "PUBLIC_BASE_URL", "EMAIL_BACKEND", "SMTP_HOST", "TELEGRAM_BOT_TOKEN",
        "NTFY_BASE_URL", "DUCKDNS_TOKEN",
    ]:  # fmt: skip
        assert name in text, name


def _gen(out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_secrets.py"), "--out", str(out), *extra],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def _parse(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_gen_secrets_writes_private_file_without_printing_secrets(tmp_path):
    out = tmp_path / ".env.prod"
    r = _gen(out)
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    values = _parse(out)
    assert len(values["JWT_SECRET"]) >= 64
    assert values["POSTGRES_PASSWORD"]
    Fernet(values["SNAPSHOT_KEY"].encode())  # raises if not a valid key
    for secret in ("JWT_SECRET", "POSTGRES_PASSWORD", "SNAPSHOT_KEY"):
        assert values[secret] not in r.stdout + r.stderr
    assert str(out) in r.stdout


def test_gen_secrets_values_are_unique_per_run(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert _gen(a).returncode == 0 and _gen(b).returncode == 0
    va, vb = _parse(a), _parse(b)
    for k in ("JWT_SECRET", "POSTGRES_PASSWORD", "SNAPSHOT_KEY"):
        assert va[k] != vb[k]


def test_gen_secrets_refuses_to_overwrite_without_force(tmp_path):
    out = tmp_path / ".env.prod"
    out.write_text("KEEP=1\n")
    r = _gen(out)
    assert r.returncode != 0
    assert out.read_text() == "KEEP=1\n"
    assert _gen(out, "--force").returncode == 0
    assert "JWT_SECRET=" in out.read_text()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
