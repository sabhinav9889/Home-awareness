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
OPTIONAL = [
    "PUBLIC_BASE_URL", "ACME_CA", "SMTP_HOST", "SMTP_PORT",
    "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
]  # fmt: skip
HAVE_DOCKER = shutil.which("docker") is not None
needs_docker = pytest.mark.skipif(not HAVE_DOCKER, reason="docker not available")


def _env(values: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in REQUIRED + OPTIONAL}
    env.update(values)
    return env


GOOD = {
    "DOMAIN": "localhost",
    "ACME_EMAIL": "ops@example.com",
    "POSTGRES_PASSWORD": "pw-for-tests",
    "JWT_SECRET": "j" * 40,
    "SNAPSHOT_KEY": Fernet.generate_key().decode(),
}


def _config(
    values: dict[str, str], *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "compose", "--env-file", "/dev/null",
            "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml",
            "-p", "ha-config-only", "config", "--format", "json",
        ],
        cwd=ROOT, env=_env({**values, **(extra_env or {})}), capture_output=True, text=True,
        timeout=60,
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
        [
            "docker", "compose", "--env-file", "/dev/null", "-f", "docker-compose.yml",
            "-p", "ha-config-only", "config", "-q",
        ],
        cwd=ROOT, env=_env({"JWT_SECRET": "x" * 40}), capture_output=True, text=True,
        timeout=60,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr


@needs_docker
def test_smtp_settings_reach_app_containers():
    smtp = {
        "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "2525", "SMTP_USER": "u",
        "SMTP_PASSWORD": "p", "SMTP_FROM": "from@example.com",
    }  # fmt: skip
    r = _config(GOOD, extra_env=smtp)
    assert r.returncode == 0, r.stderr
    services = json.loads(r.stdout)["services"]
    for name in ("api", "worker", "beat", "migrate"):
        for k, v in smtp.items():
            assert services[name]["environment"][k] == v, (name, k)


@needs_docker
def test_smtp_unset_renders_with_numeric_port(merged):
    env = merged["services"]["api"]["environment"]
    assert env["SMTP_PORT"] == "587"
    assert env["SMTP_HOST"] == ""


@needs_docker
def test_acme_ca_defaults_to_production_and_can_be_switched(merged):
    default = "https://acme-v02.api.letsencrypt.org/directory"
    assert merged["services"]["caddy"]["environment"]["ACME_CA"] == default
    staging = "https://acme-staging-v02.api.letsencrypt.org/directory"
    r = _config(GOOD, extra_env={"ACME_CA": staging})
    assert json.loads(r.stdout)["services"]["caddy"]["environment"]["ACME_CA"] == staging


def test_caddyfile_redacts_secrets_and_keeps_headers_single():
    text = (ROOT / "deploy" / "Caddyfile").read_text()
    assert "request>uri regexp" in text
    assert "(token=)[^&]*|(/telegram/webhook/)[^/?]*" in text
    assert "REDACTED" in text
    assert "?X-Content-Type-Options" in text
    assert "{$ACME_CA:" in text


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


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        ("deploy/.env.prod", True),
        ("deploy/.env.prod.bak", True),
        ("deploy/.last_good", True),
        ("deploy/.deploy.lock", True),
        ("deploy/.env.prod.example", False),
    ],
)
def test_gitignore_rules(path, ignored):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, capture_output=True)
    assert (r.returncode == 0) is ignored


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


SECRETS = ("JWT_SECRET", "POSTGRES_PASSWORD", "SNAPSHOT_KEY")


def test_gen_secrets_force_keeps_existing_secrets_and_fills_missing(tmp_path):
    out = tmp_path / ".env.prod"
    assert _gen(out).returncode == 0
    before = _parse(out)
    text = out.read_text().replace(f"SNAPSHOT_KEY={before['SNAPSHOT_KEY']}", "SNAPSHOT_KEY=")
    out.write_text(text)
    r = _gen(out, "--force")
    assert r.returncode == 0, r.stderr
    after = _parse(out)
    assert after["JWT_SECRET"] == before["JWT_SECRET"]
    assert after["POSTGRES_PASSWORD"] == before["POSTGRES_PASSWORD"]
    assert after["SNAPSHOT_KEY"] and after["SNAPSHOT_KEY"] != before["SNAPSHOT_KEY"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    for v in after.values():
        assert v not in r.stdout + r.stderr or not v


def test_gen_secrets_rotate_regenerates_everything(tmp_path):
    out = tmp_path / ".env.prod"
    assert _gen(out).returncode == 0
    before = _parse(out)
    assert _gen(out, "--rotate").returncode == 0
    after = _parse(out)
    for k in SECRETS:
        assert after[k] != before[k]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_gen_secrets_help_warns_about_rotation():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_secrets.py"), "--help"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    out = " ".join(r.stdout.split()).lower()
    assert "--rotate" in out
    for word in ("password", "snapshot", "logged out"):
        assert word in out


def test_gen_secrets_refuses_symlink_target(tmp_path):
    real = tmp_path / "real"
    real.write_text("KEEP=1\n")
    link = tmp_path / ".env.prod"
    link.symlink_to(real)
    for extra in ((), ("--force",), ("--rotate",)):
        assert _gen(link, *extra).returncode != 0
    assert real.read_text() == "KEEP=1\n"
    assert link.is_symlink()


def test_gen_secrets_failed_write_leaves_old_file_intact(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import gen_secrets
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    out = tmp_path / ".env.prod"
    out.write_text("OLD=1\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gen_secrets.os, "replace", boom)
    with pytest.raises(OSError):
        gen_secrets.main(["--out", str(out), "--force"])
    assert out.read_text() == "OLD=1\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env.prod"]  # temp file cleaned up


def test_gen_secrets_refuses_to_overwrite_without_force(tmp_path):
    out = tmp_path / ".env.prod"
    out.write_text("KEEP=1\n")
    r = _gen(out)
    assert r.returncode != 0
    assert out.read_text() == "KEEP=1\n"
    assert _gen(out, "--force").returncode == 0
    assert "JWT_SECRET=" in out.read_text()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
