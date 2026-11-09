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
        BOOTSTRAP_TESTING="1",
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


# ---------------------------------------------------------------- bootstrap: real mode (stubs)

SSH_RULE = "-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT"
OURS = [
    "-A INPUT -p tcp -m tcp --dport 80 -j ACCEPT",
    "-A INPUT -p tcp -m tcp --dport 443 -j ACCEPT",
    "-A INPUT -p udp -m udp --dport 443 -j ACCEPT",
]
IPTABLES_SIM = r"""#!/usr/bin/env bash
# Stateful iptables: only -S INPUT, -C, -I and -A are allowed; anything else is recorded as
# FORBIDDEN (the tests fail on it) and refused.
echo "iptables $*" >> "$STUB_DIR/calls.log"
S="$STUB_STATE"
case "$1" in
  -S) [ "$2" = INPUT ] || { echo "FORBIDDEN iptables $*" >> "$STUB_DIR/calls.log"; exit 99; }
      cat "$S" ;;
  -C) shift 2; grep -qxF -- "-A INPUT $*" "$S" ;;
  -I) shift 2; n=$1; shift
      awk -v n="$n" -v new="-A INPUT $*" '
        /^-A INPUT /{c++; if (c==n) {print new; done=1}} {print}
        END{if(!done) print new}' "$S" > "$S.tmp" && mv "$S.tmp" "$S" ;;
  -A) shift 2; echo "-A INPUT $*" >> "$S" ;;
  *) echo "FORBIDDEN iptables $*" >> "$STUB_DIR/calls.log"; exit 99 ;;
esac
"""
SUDO_STUB = r"""
echo "sudo $*" >> "$STUB_DIR/calls.log"
cmd=$1; shift
case "$cmd" in
  iptables) exec "$STUB_DIR/iptables-sim" "$@" ;;
  iptables-restore)
    cat > /dev/null; echo "iptables-restore $*" >> "$STUB_DIR/calls.log"
    [ -f "$STUB_DIR/restore_fail" ] && exit 1; exit 0 ;;
  netfilter-persistent)
    echo "FORBIDDEN netfilter-persistent $*" >> "$STUB_DIR/calls.log"; exit 99 ;;
  cat|install|cp|chmod|stat|mv|awk|grep|test|rm)
    for a in "$@"; do
      case "$a" in /etc/*) echo "sudo stub: real /etc touched: $*" >&2; exit 99 ;; esac
    done
    exec "$cmd" "$@" ;;
  *) exit 0 ;;
esac
"""


def _machine(
    stubs: Path,
    tmp_path: Path,
    *,
    rules: str = ORACLE_RULES,
    rules_v4: str | None = "",
    session_in_group: bool = True,
    members: str = "ubuntu",
    ufw: bool = False,
    missing: str = "",
) -> dict:
    """A machine that is otherwise fully bootstrapped. rules_v4='' means: same as the live rules."""
    etc = tmp_path / "etc"
    (etc / "apt/apt.conf.d").mkdir(parents=True, exist_ok=True)
    (etc / "iptables").mkdir(exist_ok=True)
    (etc / "apt/apt.conf.d/20auto-upgrades").write_text(
        'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'
    )
    (etc / "fstab").write_text("/swapfile none swap sw 0 0\n")
    swaps = tmp_path / "swaps"
    swaps.write_text("Filename Type Size Used Priority\n/swapfile file 2097148 0 -2\n")
    state = tmp_path / "iptables-state"
    state.write_text(rules)
    if rules_v4 is not None:
        body = rules_v4 or (
            "*filter\n" + "".join(ln + "\n" for ln in rules.splitlines() if ln.startswith("-A"))
        )
        v4 = etc / "iptables/rules.v4"
        v4.write_text(body if body.endswith("COMMIT\n") else body + "COMMIT\n")
        v4.chmod(0o640)
    _stub(stubs / "iptables-sim", IPTABLES_SIM.split("\n", 1)[1])
    _stub(stubs / "sudo", SUDO_STUB)
    _stub(
        stubs / "dpkg-query",
        'pkg="${@: -1}"\n'
        'if [ "$pkg" = ufw ]; then\n'
        '  [ "${STUB_UFW:-0}" = 1 ] && { echo "install ok installed"; exit 0; }\n'
        "  exit 1\nfi\n"
        'case " $STUB_MISSING " in *" $pkg "*) exit 1 ;; esac\n'
        "echo 'install ok installed'\n",
    )
    _stub(stubs / "docker", 'echo "docker $*" >> "$STUB_DIR/calls.log"\necho 2.30.0\n')
    groups = "ubuntu docker" if session_in_group else "ubuntu"
    _stub(
        stubs / "id",
        f'case "$1" in -u) exec /usr/bin/id -u ;; -un) echo ubuntu ;; *) echo "{groups}" ;; esac\n',
    )
    _stub(stubs / "getent", 'echo "docker:x:999:$STUB_MEMBERS"\n')
    _stub(
        stubs / "systemctl",
        'echo "systemctl $*" >> "$STUB_DIR/calls.log"\n'
        '[ "$1" = is-enabled ] && echo enabled\nexit 0\n',
    )
    return {
        "BOOTSTRAP_ETC": str(etc),
        "BOOTSTRAP_PROC_SWAPS": str(swaps),
        "STUB_STATE": str(state),
        "STUB_MEMBERS": members,
        "STUB_UFW": "1" if ufw else "0",
        "STUB_MISSING": missing,
    }


def _v4(extra) -> Path:
    return Path(extra["BOOTSTRAP_ETC"]) / "iptables/rules.v4"


def _state(extra) -> list[str]:
    return Path(extra["STUB_STATE"]).read_text().splitlines()


def _assert_clean(stubs: Path) -> None:
    log = "\n".join(_calls(stubs))
    assert "FORBIDDEN" not in log
    assert "netfilter-persistent" not in log  # never save or reload: Docker's chains stay out
    assert "iptables -F" not in log and "iptables -X" not in log and "iptables -P" not in log


def _before(lines: list[str], rule: str, anchor_prefix: str) -> bool:
    return lines.index(rule) < next(i for i, ln in enumerate(lines) if ln.startswith(anchor_prefix))


def test_second_run_changes_nothing_and_says_so(stubs, tmp_path):
    extra = _machine(stubs, tmp_path, rules=_with_ours(ORACLE_RULES))
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    assert "nothing to change" in r.stdout.lower()
    log = "\n".join(_calls(stubs))
    for banned in ("apt-get", "usermod", "tee", " dd ", "mkswap", "swapon", "iptables -I"):
        assert banned not in log, banned
    assert "sudo iptables -C" in log
    _assert_clean(stubs)


def _with_ours(rules: str, anchor: str = "-A INPUT -j REJECT") -> str:
    return rules.replace(anchor, "\n".join(OURS) + "\n" + anchor)


def test_insert_then_rerun_is_idempotent(stubs, tmp_path):
    extra = _machine(stubs, tmp_path, rules=ORACLE_RULES)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    state = _state(extra)
    assert state[-1].startswith("-A INPUT -j REJECT")
    assert state[1:5] == state[1:5] and all(o in state for o in OURS)
    for o in OURS:
        assert _before(state, o, "-A INPUT -j REJECT")
    assert SSH_RULE in state and SSH_RULE in _v4(extra).read_text()
    (stubs / "calls.log").write_text("")
    r2 = _run([], stubs, tmp_path, **extra)
    assert "nothing to change" in r2.stdout.lower()
    assert not [c for c in _calls(stubs) if " -I " in c or " -A " in c]
    _assert_clean(stubs)


def test_inserts_go_before_the_reject_rule_and_are_persisted_atomically(stubs, tmp_path):
    filt = "".join(ln + "\n" for ln in ORACLE_RULES.splitlines() if ln.startswith("-A"))
    v4_text = "*nat\n:DOCKER - [0:0]\nCOMMIT\n*filter\n" + filt
    extra = _machine(stubs, tmp_path, rules=ORACLE_RULES, rules_v4=v4_text)
    v4 = _v4(extra)
    v4.chmod(0o600)
    original = v4.read_text()
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    inserts = [c for c in _calls(stubs) if c.startswith("sudo iptables -I INPUT")]
    # each insert goes just before REJECT, which moves down by one every time
    assert [c.split()[4] for c in inserts] == ["5", "6", "7"]
    lines = v4.read_text().splitlines()
    for o in OURS:
        assert _before(lines, o, "-A INPUT -j REJECT")
    assert lines[:3] == ["*nat", ":DOCKER - [0:0]", "COMMIT"]  # other tables untouched
    assert v4.stat().st_mode & 0o777 == 0o600  # mode kept
    assert (v4.parent / "rules.v4.ha-bak").read_text() == original
    assert not list(v4.parent.glob("*.new*"))  # temp file renamed away
    log = _calls(stubs)
    assert any(c.startswith("iptables-restore --test") for c in log)  # validated before the move
    _assert_clean(stubs)


def test_existing_backup_is_never_overwritten(stubs, tmp_path):
    extra = _machine(stubs, tmp_path, rules=ORACLE_RULES)
    (_v4(extra).parent / "rules.v4.ha-bak").write_text("OLD\n")
    assert _run([], stubs, tmp_path, **extra).returncode == 0
    assert (_v4(extra).parent / "rules.v4.ha-bak").read_text() == "OLD\n"


def test_failed_restore_test_leaves_rules_file_untouched(stubs, tmp_path):
    extra = _machine(stubs, tmp_path, rules=ORACLE_RULES)
    original = _v4(extra).read_text()
    (stubs / "restore_fail").write_text("")
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode != 0
    assert _v4(extra).read_text() == original
    assert not list(_v4(extra).parent.glob("*.new*"))


def test_missing_rules_file_is_created_from_the_input_chain_only(stubs, tmp_path):
    extra = _machine(stubs, tmp_path, rules=ORACLE_RULES, rules_v4=None)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    v4 = _v4(extra)
    lines = v4.read_text().splitlines()
    assert lines[0] == "*filter" and lines[-1] == "COMMIT"
    assert any(ln.startswith(":INPUT ACCEPT") for ln in lines)
    assert SSH_RULE in lines
    for o in OURS:
        assert _before(lines, o, "-A INPUT -j REJECT")
    assert not any("DOCKER" in ln for ln in lines)
    assert v4.stat().st_mode & 0o777 == 0o640
    _assert_clean(stubs)


def test_file_without_reject_gets_the_rules_at_the_end_of_the_input_section(stubs, tmp_path):
    live = "-P INPUT ACCEPT\n-A INPUT -i lo -j ACCEPT\n" + SSH_RULE + "\n"
    v4 = (
        "*filter\n:INPUT ACCEPT [0:0]\n:FORWARD ACCEPT [0:0]\n-A INPUT -i lo -j ACCEPT\n"
        + SSH_RULE
        + "\n-A FORWARD -j DOCKER-USER\nCOMMIT\n*nat\n-A POSTROUTING -j MASQUERADE\nCOMMIT\n"
    )
    extra = _machine(stubs, tmp_path, rules=live, rules_v4=v4)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    lines = _v4(extra).read_text().splitlines()
    i = lines.index(SSH_RULE)
    assert lines[i + 1 : i + 4] == OURS
    assert lines[i + 4] == "-A FORWARD -j DOCKER-USER"
    assert lines[-2:] == ["-A POSTROUTING -j MASQUERADE", "COMMIT"]
    assert _state(extra)[-3:] == OURS
    _assert_clean(stubs)


def test_chain_ending_in_drop_gets_the_rules_before_the_drop(stubs, tmp_path):
    live = ORACLE_RULES.replace(
        "-A INPUT -j REJECT --reject-with icmp-host-prohibited", "-A INPUT -j DROP"
    )
    extra = _machine(stubs, tmp_path, rules=live)  # fmt: skip
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    for text in ("\n".join(_state(extra)), _v4(extra).read_text()):
        lines = text.splitlines()
        for o in OURS:
            assert _before(lines, o, "-A INPUT -j DROP")
    _assert_clean(stubs)


def test_no_safe_insertion_point_stops_with_a_clear_error(stubs, tmp_path):
    live = "-P INPUT ACCEPT\n" + SSH_RULE + "\n-A INPUT -p tcp -m tcp --dport 9 -j REJECT\n"
    extra = _machine(stubs, tmp_path, rules=live)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode != 0
    assert "firewall" in r.stderr.lower()
    assert "done: opened" not in r.stdout
    assert not [c for c in _calls(stubs) if " -I " in c or " -A " in c]


def test_ufw_present_skips_the_firewall_step_with_instructions(stubs, tmp_path):
    extra = _machine(stubs, tmp_path, rules=ORACLE_RULES, ufw=True)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    for cmd in ("ufw allow 80/tcp", "ufw allow 443/tcp", "ufw allow 443/udp"):
        assert cmd in out
    log = "\n".join(_calls(stubs))
    assert "iptables" not in log and "apt-get" not in log
    assert not _v4(extra).with_name("rules.v4.ha-bak").exists()


def test_group_membership_comes_from_the_group_database_not_the_session(stubs, tmp_path):
    extra = _machine(
        stubs, tmp_path, rules=_with_ours(ORACLE_RULES), session_in_group=False, members="x,ubuntu"
    )
    r = _run([], stubs, tmp_path, **extra)
    assert "nothing to change" in r.stdout.lower()
    assert not [c for c in _calls(stubs) if "usermod" in c]


def test_user_not_in_group_is_added(stubs, tmp_path):
    extra = _machine(stubs, tmp_path, rules=_with_ours(ORACLE_RULES), members="root")
    r = _run([], stubs, tmp_path, **extra)
    assert "sudo usermod -aG docker ubuntu" in _calls(stubs)
    assert "nothing to change" not in r.stdout.lower()


def test_assumed_uid_is_ignored_without_the_testing_flag(stubs, tmp_path):
    r = _run(["--dry-run"], stubs, tmp_path, BOOTSTRAP_ASSUME_UID="0", BOOTSTRAP_TESTING="")
    if os.geteuid() != 0:
        assert r.returncode == 0, r.stderr  # the real uid (not 0) decides


def test_assumed_uid_must_be_digits(stubs, tmp_path):
    r = _run(["--dry-run"], stubs, tmp_path, BOOTSTRAP_ASSUME_UID="abc")
    assert r.returncode == 2


FPR = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
KEY_ONE = (
    f"tru::1:1:1:0:3:1:5\npub:-:4096:1:8D81803C0EBFCD88:1:::-:::scESC:::::::\nfpr:::::::::{FPR}:\n"
)
KEY_TWO = (
    KEY_ONE + "pub:-:4096:1:AAAAAAAAAAAAAAAA:1:::-:::scESC:::::::\nfpr:::::::::" + "A" * 40 + ":\n"
)


def _key_machine(stubs, tmp_path, colon: str) -> dict:
    extra = _machine(stubs, tmp_path, rules=_with_ours(ORACLE_RULES), missing="docker-ce")
    (stubs / "colon").write_text(colon)
    (Path(extra["BOOTSTRAP_ETC"]) / "apt/keyrings").mkdir()
    (Path(extra["BOOTSTRAP_ETC"]) / "apt/keyrings/docker.gpg").write_text("k")
    _stub(stubs / "gpg", 'echo "gpg $*" >> "$STUB_DIR/calls.log"\ncat "$STUB_DIR/colon"\n')
    _stub(
        stubs / "curl",
        'echo "curl $*" >> "$STUB_DIR/calls.log"\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\n',
    )
    return extra


def test_docker_key_file_with_extra_keys_is_rejected(stubs, tmp_path):
    extra = _key_machine(stubs, tmp_path, KEY_TWO)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode != 0
    assert "exactly one" in r.stderr
    log = "\n".join(_calls(stubs))
    assert "dearmor" not in log and "apt-get install" not in log


def test_docker_key_single_matching_key_is_accepted(stubs, tmp_path):
    extra = _key_machine(stubs, tmp_path, KEY_ONE)
    r = _run([], stubs, tmp_path, **extra)
    assert r.returncode == 0, r.stderr
    assert "dearmor" in "\n".join(_calls(stubs))


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
        DUCKDNS_IDLE_INTERVAL="0", DUCKDNS_IDLE_MAX_LOOPS="1",
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


def test_duckdns_bad_domain_idles_with_one_clear_message_and_exits_zero(stubs, tmp_path):
    r = _duck(tmp_path, stubs, ["OK"], DOMAIN="home.example.com")
    assert r.returncode == 0  # a config error must never fail `up -d --wait` (and so a deploy)
    assert "duckdns.org" in r.stderr and "DUCKDNS_SUBDOMAIN" in r.stderr
    assert r.stderr.count("DUCKDNS_SUBDOMAIN") == 1
    assert not (stubs / "curl-argv.log").exists()
    assert TOKEN not in r.stdout + r.stderr


def test_duckdns_config_error_repeats_the_message_while_idle(stubs, tmp_path):
    r = _duck(tmp_path, stubs, ["OK"], DOMAIN="home.example.com", DUCKDNS_IDLE_MAX_LOOPS="3")
    assert r.stderr.count("DUCKDNS_SUBDOMAIN") == 3


def test_duckdns_requires_a_token_but_idles(stubs, tmp_path):
    r = _duck(tmp_path, stubs, ["OK"], DUCKDNS_TOKEN="")
    assert r.returncode == 0 and "DUCKDNS_TOKEN" in r.stderr


@pytest.mark.parametrize("bad", ['a"b', "a b", "a/b", "a.b", "a&b=c"])
def test_duckdns_rejects_unsafe_values_without_echoing_them(stubs, tmp_path, bad):
    r = _duck(tmp_path, stubs, ["OK"], DUCKDNS_TOKEN=bad + TOKEN)
    assert r.returncode == 0 and r.stderr
    assert TOKEN not in r.stdout + r.stderr
    r = _duck(tmp_path, stubs, ["OK"], DUCKDNS_SUBDOMAIN=bad)
    assert r.returncode == 0 and r.stderr
    assert not (stubs / "curl-argv.log").exists()


def test_duckdns_idle_exits_zero_on_term(stubs, tmp_path):
    import signal

    env = {k: v for k, v in os.environ.items() if not k.startswith(("DUCKDNS_", "DOMAIN"))} | {
        "DOMAIN": "home.example.com",
        "DUCKDNS_TOKEN": TOKEN,
    }
    p = subprocess.Popen(
        ["sh", str(DUCKDNS)], env=env, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    first = p.stderr.readline()
    assert "DUCKDNS_SUBDOMAIN" in first
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0
    assert TOKEN not in first + p.stderr.read()


def test_duckdns_forces_ipv4(stubs, tmp_path):
    _duck(tmp_path, stubs, ["OK"])
    assert " -4 " in (stubs / "curl-argv.log").read_text()


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
    assert svc["init"] is True
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


def _image_present(name: str) -> bool:
    return (
        HAVE_DOCKER
        and subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode
        == 0
    )


@pytest.mark.skipif(not _image_present("curlimages/curl:8.11.1"), reason="curl image not local")
def test_misconfigured_duckdns_never_breaks_up_wait(tmp_path):
    """Real throwaway compose project: a bad DOMAIN must leave the service running, so a deploy's
    `up -d --wait` keeps succeeding (it used to restart-loop and flap)."""
    env = {k: v for k, v in os.environ.items() if k not in _COMPOSE_ENV}
    env.update(_COMPOSE_ENV | {"DOMAIN": "home.example.com", "DUCKDNS_TOKEN": ""})
    base = [
        "docker", "compose", "--env-file", "/dev/null", "-f", "docker-compose.yml",
        "-f", "docker-compose.prod.yml", "--profile", "duckdns", "-p", "ha-deploy-test",
    ]  # fmt: skip
    try:
        for i in range(6):
            r = subprocess.run(
                [*base, "up", "-d", "--wait", "--wait-timeout", "60", "--no-deps",
                 "--force-recreate", "duckdns"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
            )  # fmt: skip
            assert r.returncode == 0, (i, r.stderr)
        logs = subprocess.run(
            [*base, "logs", "duckdns"], cwd=ROOT, env=env, capture_output=True, text=True
        ).stdout
        assert "DUCKDNS_SUBDOMAIN" in logs
    finally:
        subprocess.run(
            [*base, "down", "-v", "--remove-orphans", "-t", "1"],
            cwd=ROOT, env=env, capture_output=True, timeout=120,
        )  # fmt: skip


def test_caddyfile_hides_metrics():
    text = (ROOT / "deploy" / "Caddyfile").read_text()
    assert "path /metrics /metrics/*" in text
    assert "respond" in text and "404" in text
    assert text.index("/metrics") < text.index("reverse_proxy")
