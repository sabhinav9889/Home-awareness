"""Tests for deploy/bootstrap-vm.sh and the DuckDNS updater.

Every external command is a recording stub on PATH. Nothing here runs apt, sudo, iptables,
systemctl or a real Docker install, and nothing calls DuckDNS or any network.
"""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "deploy" / "bootstrap-vm.sh"
DUCKDNS = ROOT / "deploy" / "duckdns.sh"
TOKEN = "t0ken-SECRET-0000-must-never-print"
HAVE_DOCKER = shutil.which("docker") is not None
needs_docker = pytest.mark.skipif(not HAVE_DOCKER, reason="docker not available")

# Commands that change the machine or reach the network: none may run in a dry run.
MUTATING = [
    "apt-get", "sudo", "iptables", "systemctl", "tee", "usermod", "curl", "gpg", "dd",
    "mkswap", "swapon", "netfilter-persistent", "fallocate",
]  # fmt: skip

OS_UBUNTU = 'ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n'

ORACLE_RULES = """\
-P INPUT ACCEPT
-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
-A INPUT -p icmp -j ACCEPT
-A INPUT -i lo -j ACCEPT
-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
-A INPUT -j REJECT --reject-with icmp-host-prohibited
"""


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def stubs(tmp_path):
    d = tmp_path / "stubs"
    d.mkdir()
    (d / "calls.log").touch()
    for name in MUTATING:
        _stub(d / name, f'echo "{name} $*" >> "$STUB_DIR/calls.log"\nexit 0\n')
    _stub(d / "uname", 'if [ "${1:-}" = "-m" ]; then echo "${STUB_UNAME_M:-aarch64}"; exit 0; fi\n'
          'exec /usr/bin/uname "$@"\n')  # fmt: skip
    return d


def _env(stubs: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    osr = tmp_path / "os-release"
    if not osr.exists():
        osr.write_text(OS_UBUNTU)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BOOTSTRAP_", "STUB_"))}
    env.update(
        PATH=f"{stubs}:{env['PATH']}",
        STUB_DIR=str(stubs),
        BOOTSTRAP_OS_RELEASE=str(osr),
        BOOTSTRAP_ASSUME_UID="1000",
        HOME=str(tmp_path),
    )
    env.update(extra)
    return env


def _run(args, stubs, tmp_path, **extra):
    return subprocess.run(
        ["bash", str(BOOTSTRAP), *args], env=_env(stubs, tmp_path, **extra),
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )  # fmt: skip


def _calls(stubs: Path) -> list[str]:
    return (stubs / "calls.log").read_text().splitlines()


# ---------------------------------------------------------------- bootstrap: dry run


def test_dry_run_prints_every_step_and_runs_nothing_mutating(stubs, tmp_path):
    r = _run(["--dry-run"], stubs, tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for needle in [
        "would: add Docker's apt repository",
        "would: install docker-ce",
        "would: add",  # docker group
        "would: open 80/tcp",
        "would: open 443/tcp",
        "would: open 443/udp",
        "would: persist",
        "would: install unattended-upgrades",
        "would: create a 2 GB swap file",
        "gen_secrets.py",
        "deploy/.env.prod",
        "deploy/deploy.sh",
    ]:
        assert needle in out, needle
    assert _calls(stubs) == []


def test_dry_run_allows_only_the_readonly_compose_version_probe(stubs, tmp_path):
    _stub(stubs / "docker", 'echo "docker $*" >> "$STUB_DIR/calls.log"\necho 2.29.1\n')
    r = _run(["--dry-run"], stubs, tmp_path)
    assert r.returncode == 0, r.stderr
    assert _calls(stubs) == ["docker compose version --short"]


def test_dry_run_changes_no_files(stubs, tmp_path):
    _env(stubs, tmp_path)  # creates the fake os-release
    before = sorted(p.name for p in tmp_path.iterdir())
    _run(["--dry-run"], stubs, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_refuses_root(stubs, tmp_path):
    r = _run(["--dry-run"], stubs, tmp_path, BOOTSTRAP_ASSUME_UID="0")
    assert r.returncode != 0
    assert "root" in r.stderr and "sudo" in r.stderr
    assert _calls(stubs) == []


def test_refuses_non_ubuntu(stubs, tmp_path):
    (tmp_path / "os-release").write_text('ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n')
    r = _run(["--dry-run"], stubs, tmp_path)
    assert r.returncode != 0
    assert "Ubuntu" in r.stderr
    assert "would:" not in r.stdout


def test_unknown_ubuntu_release_is_a_warning(stubs, tmp_path):
    (tmp_path / "os-release").write_text('ID=ubuntu\nVERSION_ID="20.04"\nVERSION_CODENAME=focal\n')
    r = _run(["--dry-run"], stubs, tmp_path)
    assert r.returncode == 0
    assert "22.04" in r.stderr and "24.04" in r.stderr


def test_unsupported_architecture_is_refused(stubs, tmp_path):
    r = _run(["--dry-run"], stubs, tmp_path, STUB_UNAME_M="armv7l")
    assert r.returncode != 0
    assert "armv7l" in r.stderr


def test_x86_is_accepted_with_a_memory_warning(stubs, tmp_path):
    r = _run(["--dry-run"], stubs, tmp_path, STUB_UNAME_M="x86_64")
    assert r.returncode == 0, r.stderr
    assert "x86_64" in r.stderr and "1 GB" in r.stderr
    assert "arch=amd64" in r.stdout


def test_dry_run_uses_arm64_and_the_release_codename(stubs, tmp_path):
    r = _run(["--dry-run"], stubs, tmp_path)
    assert "arch=arm64" in r.stdout and "noble" in r.stdout


def test_old_compose_is_reported(stubs, tmp_path):
    _stub(stubs / "docker", "echo 2.20.0\n")
    r = _run(["--dry-run"], stubs, tmp_path)
    assert r.returncode != 0
    assert "2.24" in r.stderr and "2.20.0" in r.stderr


def test_unknown_option_is_rejected(stubs, tmp_path):
    r = _run(["--bogus"], stubs, tmp_path)
    assert r.returncode == 2


def test_script_pins_the_published_docker_key_fingerprint():
    text = BOOTSTRAP.read_text()
    assert "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" in text
    assert "docs.docker.com/engine/install/ubuntu" in text


# ---------------------------------------------------------------- bootstrap: real mode


def _satisfied_machine(stubs: Path, tmp_path: Path, *, in_group=True, rules_ok=True) -> dict:
    """Stubs and a fake /etc for a machine that is already fully bootstrapped."""
    etc = tmp_path / "etc"
    (etc / "apt/apt.conf.d").mkdir(parents=True)
    (etc / "iptables").mkdir()
    (etc / "apt/apt.conf.d/20auto-upgrades").write_text(
        'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
    )
    rules = ORACLE_RULES.replace(
        "-A INPUT -j REJECT",
        "-A INPUT -p tcp -m tcp --dport 80 -j ACCEPT\n"
        "-A INPUT -p tcp -m tcp --dport 443 -j ACCEPT\n"
        "-A INPUT -p udp -m udp --dport 443 -j ACCEPT\n-A INPUT -j REJECT",
    )
    (etc / "iptables/rules.v4").write_text("*filter\n" + rules + "COMMIT\n")
    (etc / "fstab").write_text("/swapfile none swap sw 0 0\n")
    swaps = tmp_path / "swaps"
    swaps.write_text("Filename Type Size Used Priority\n/swapfile file 2097148 0 -2\n")
    state = tmp_path / "iptables-state"
    state.write_text(rules if rules_ok else ORACLE_RULES)
    _stub(stubs / "dpkg-query", "echo 'install ok installed'\n")
    _stub(stubs / "docker", 'echo "docker $*" >> "$STUB_DIR/calls.log"\necho 2.30.0\n')
    _stub(stubs / "id", 'echo "ubuntu%s"\n' % (" docker" if in_group else ""))
    _stub(
        stubs / "sudo",
        """
        echo "sudo $*" >> "$STUB_DIR/calls.log"
        case "$1" in
          iptables)
            case "$2" in
              -C) shift 2; grep -qxF -- "-A $*" "$STUB_STATE"; exit $? ;;
              -S) cat "$STUB_STATE"; exit 0 ;;
              *) exit 0 ;;
            esac ;;
          cat|install|cp|chmod|awk|grep|test|usermod|tee|systemctl|apt-get|netfilter-persistent|dd|mkswap|swapon|gpg|fallocate|env)
            for a in "$@"; do
              case "$a" in /etc/*) echo "sudo stub: real /etc touched: $*" >&2; exit 99 ;; esac
            done
            case "$1" in cat|install|cp|chmod|awk|grep|test) exec "$@" ;; *) exit 0 ;; esac ;;
          *) exit 0 ;;
        esac
        """,
    )
    _stub(
        stubs / "systemctl",
        'echo "systemctl $*" >> "$STUB_DIR/calls.log"\n'
        '[ "$1" = is-enabled ] && echo enabled\nexit 0\n',
    )
    return {
        "BOOTSTRAP_ETC": str(etc),
        "BOOTSTRAP_PROC_SWAPS": str(swaps),
        "STUB_STATE": str(state),
    }


def test_second_run_changes_nothing_and_says_so(stubs, tmp_path):
    extra = _satisfied_machine(stubs, tmp_path)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    assert "nothing to change" in r.stdout.lower()
    mutating = ("apt-get", "usermod", "tee", "dd", "mkswap", "swapon", "install", "-I", "-A")
    for line in _calls(stubs):
        assert not any(f" {m} " in f" {line} " or line.startswith(m) for m in mutating), line
    assert "sudo iptables -C" in "\n".join(_calls(stubs))


def test_missing_docker_group_is_the_only_change(stubs, tmp_path):
    extra = _satisfied_machine(stubs, tmp_path, in_group=False)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    assert "sudo usermod -aG docker ubuntu" in _calls(stubs)
    assert "nothing to change" not in r.stdout.lower()
    assert not [c for c in _calls(stubs) if c.startswith("sudo apt-get")]


def test_missing_rule_is_inserted_before_reject_and_ssh_untouched(stubs, tmp_path):
    extra = _satisfied_machine(stubs, tmp_path, rules_ok=False)
    # persisted file lacks the rules too
    (Path(extra["BOOTSTRAP_ETC"]) / "iptables/rules.v4").write_text(
        "*filter\n" + ORACLE_RULES + "COMMIT\n"
    )
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    inserts = [c for c in _calls(stubs) if c.startswith("sudo iptables -I INPUT")]
    assert len(inserts) == 3
    # REJECT is the 5th INPUT rule (the stub state does not change), so each insert goes to 5
    assert all(c.startswith("sudo iptables -I INPUT 5 ") for c in inserts)
    assert all("--dport 22" not in c for c in _calls(stubs) if " -I " in c or " -D " in c)
    assert not [c for c in _calls(stubs) if "iptables -F" in c or "iptables -D" in c]
    persisted = (Path(extra["BOOTSTRAP_ETC"]) / "iptables/rules.v4").read_text().splitlines()
    reject = next(i for i, ln in enumerate(persisted) if ln.startswith("-A INPUT -j REJECT"))
    for rule in (
        "-A INPUT -p tcp -m tcp --dport 80 -j ACCEPT",
        "-A INPUT -p tcp -m tcp --dport 443 -j ACCEPT",
        "-A INPUT -p udp -m udp --dport 443 -j ACCEPT",
    ):
        assert persisted.index(rule) < reject
    assert "-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT" in persisted
    assert (Path(extra["BOOTSTRAP_ETC"]) / "iptables/rules.v4.ha-bak").exists()


# ---------------------------------------------------------------- health endpoint


def test_health_needs_no_auth_and_reveals_only_status_keys():
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app
    from app.redis import get_redis

    class FakeSession:
        async def execute(self, *_a, **_k):
            return None

    class FakeRedis:
        async def ping(self):
            return True

    async def session():
        yield FakeSession()

    async def redis():
        return FakeRedis()

    app = create_app()
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_redis] = redis
    r = TestClient(app).get("/health")  # no Authorization header
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "redis": "ok"}


# ---------------------------------------------------------------- duckdns updater

CURL_STUB = r"""#!/usr/bin/env bash
echo "curl $*" >> "$STUB_DIR/curl-argv.log"
cat >> "$STUB_DIR/curl-stdin.log"
n=$(cat "$STUB_DIR/n" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$STUB_DIR/n"
line=$(sed -n "${n}p" "$STUB_DIR/responses")
[ -n "$line" ] || line=$(tail -n 1 "$STUB_DIR/responses")
case "$line" in
  FAIL) echo "curl: (6) Could not resolve host" >&2; exit 6 ;;
  *) printf '%s\n' "$line" ;;
esac
"""


def _duck(tmp_path, stubs, responses, *, loops=None, **env_extra):
    (stubs / "responses").write_text("\n".join(responses) + "\n")
    _stub(stubs / "curl", CURL_STUB.split("\n", 1)[1])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DUCKDNS_", "DOMAIN"))}
    env.update(
        PATH=f"{stubs}:{env['PATH']}", STUB_DIR=str(stubs), DUCKDNS_INTERVAL="0",
        DUCKDNS_MAX_LOOPS=str(loops or len(responses)),
        DOMAIN="myhome.duckdns.org", DUCKDNS_TOKEN=TOKEN,
    )  # fmt: skip
    env.update(env_extra)
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run(
        ["sh", str(DUCKDNS)], env=env, capture_output=True, text=True, timeout=30, cwd=tmp_path
    )


def test_duckdns_logs_only_state_changes(stubs, tmp_path):
    r = _duck(tmp_path, stubs, ["OK", "OK", "KO", "KO", "OK", "OK"])
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 3, lines
    assert "OK" in lines[0] and "KO" in lines[1] and "OK" in lines[2]
    assert "myhome" in lines[0]


def test_duckdns_curl_failure_counts_as_ko_and_is_logged_once(stubs, tmp_path):
    r = _duck(tmp_path, stubs, ["OK", "FAIL", "FAIL", "OK"])
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 3, lines
    assert "KO" in lines[1]


def test_duckdns_token_never_appears_in_output_or_argv(stubs, tmp_path):
    r = _duck(tmp_path, stubs, ["OK", "KO", "FAIL", "OK"])
    assert TOKEN not in r.stdout and TOKEN not in r.stderr
    argv = (stubs / "curl-argv.log").read_text()
    assert argv and TOKEN not in argv
    assert "-K -" in argv or "--config -" in argv
    stdin = (stubs / "curl-stdin.log").read_text()
    assert f"token={TOKEN}" in stdin  # it does reach curl, but only through stdin
    assert "domains=myhome&" in stdin and "ip=" in stdin


def test_duckdns_subdomain_override(stubs, tmp_path):
    _duck(tmp_path, stubs, ["OK"], DOMAIN="home.example.com", DUCKDNS_SUBDOMAIN="other")
    assert "domains=other&" in (stubs / "curl-stdin.log").read_text()


def test_duckdns_requires_a_duckdns_domain_or_subdomain(stubs, tmp_path):
    r = _duck(tmp_path, stubs, ["OK"], DOMAIN="home.example.com")
    assert r.returncode != 0
    assert "duckdns.org" in r.stderr and "DUCKDNS_SUBDOMAIN" in r.stderr
    assert not (stubs / "curl-argv.log").exists()
    assert TOKEN not in r.stdout + r.stderr


def test_duckdns_requires_a_token(stubs, tmp_path):
    r = _duck(tmp_path, stubs, ["OK"], DUCKDNS_TOKEN="")
    assert r.returncode != 0 and "DUCKDNS_TOKEN" in r.stderr


@pytest.mark.parametrize("bad", ['a"b', "a b", "a/b", "a.b", "a&b=c"])
def test_duckdns_rejects_unsafe_values_without_echoing_them(stubs, tmp_path, bad):
    r = _duck(tmp_path, stubs, ["OK"], DUCKDNS_TOKEN=bad + TOKEN)
    assert r.returncode != 0
    assert TOKEN not in r.stdout + r.stderr
    r = _duck(tmp_path, stubs, ["OK"], DUCKDNS_SUBDOMAIN=bad)
    assert r.returncode != 0
    assert not (stubs / "curl-argv.log").exists()


# ---------------------------------------------------------------- compose service

_COMPOSE_ENV = {
    "DOMAIN": "myhome.duckdns.org", "ACME_EMAIL": "ops@example.com",
    "POSTGRES_PASSWORD": "pw", "JWT_SECRET": "j" * 40, "SNAPSHOT_KEY": "k" * 44,
    "DUCKDNS_TOKEN": TOKEN,
}  # fmt: skip


def _compose(*profile_args: str):
    env = {k: v for k, v in os.environ.items() if k not in _COMPOSE_ENV and k != "COMPOSE_PROFILES"}
    env.update(_COMPOSE_ENV)
    return subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "-f", "docker-compose.yml",
         "-f", "docker-compose.prod.yml", "-p", "ha-config-only", *profile_args,
         "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip


@needs_docker
def test_duckdns_service_is_optional_and_renders():
    off = _compose()
    assert off.returncode == 0, off.stderr
    assert "duckdns" not in json.loads(off.stdout)["services"]
    on = _compose("--profile", "duckdns")
    assert on.returncode == 0, on.stderr
    svc = json.loads(on.stdout)["services"]["duckdns"]
    assert svc["restart"] == "unless-stopped"
    assert not svc.get("ports")
    assert svc["profiles"] == ["duckdns"]
    assert svc["logging"]["driver"] == "json-file"
    assert svc["environment"]["DUCKDNS_TOKEN"] == TOKEN
    assert "/deploy/duckdns.sh" in svc["entrypoint"]


@needs_docker
def test_compose_profiles_in_the_env_file_enables_duckdns(tmp_path):
    envf = tmp_path / "env"
    envf.write_text(
        "".join(f"{k}={v}\n" for k, v in _COMPOSE_ENV.items()) + "COMPOSE_PROFILES=duckdns\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in _COMPOSE_ENV | {"COMPOSE_PROFILES": 1}}
    r = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml",
         "--env-file", str(envf), "-p", "ha-config-only", "config", "--services"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "duckdns" in r.stdout.split()


def test_duckdns_service_text_has_no_ports_and_a_multiarch_image():
    text = (ROOT / "docker-compose.prod.yml").read_text()
    block = text.split("  duckdns:\n", 1)[1].split("\nvolumes:", 1)[0]
    block = block.split("\n  backup:", 1)[0]
    assert "ports:" not in block
    assert "image: curlimages/curl:" in block  # multi-arch, runs on arm64


# ---------------------------------------------------------------- lint


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_passes():
    for script, shell in ((BOOTSTRAP, "bash"), (DUCKDNS, "sh")):
        r = subprocess.run(
            ["shellcheck", "-s", shell, str(script)], capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0, r.stdout


def test_scripts_are_executable_with_strict_mode():
    for script in (BOOTSTRAP, DUCKDNS):
        assert os.access(script, os.X_OK), script
    assert "set -euo pipefail" in BOOTSTRAP.read_text()
    assert "set -eu" in DUCKDNS.read_text()
