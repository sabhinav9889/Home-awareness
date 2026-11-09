"""Deploy script and workflow tests. Everything external (docker, curl, flock) is a stub on PATH.

Git is the real binary but only ever runs inside throwaway repositories under tmp_path.
The real Docker daemon, the network and SSH are never used.
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = ROOT / "deploy" / "deploy.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
SECRET = "s3cr3t-VALUE-must-never-print-9f8e7d"
BASE_PATH = "/usr/bin:/bin"

DOCKER_STUB = r"""#!/usr/bin/env bash
head=$(git rev-parse --short HEAD 2>/dev/null || echo none)
echo "docker $* @$head" >> "$STUB_DIR/calls.log"
if [[ " $* " == *" version "* ]]; then
  cat "$STUB_DIR/compose_version" 2>/dev/null || echo "v2.30.0"
fi
if [ -f "$STUB_DIR/slow" ] && [[ " $* " == *" build "* ]]; then sleep 2; fi
if [ -f "$STUB_DIR/docker_fail" ]; then
  while read -r sha pat; do
    [ -z "$sha" ] && continue
    full=$(git rev-parse HEAD 2>/dev/null || echo none)
    if { [ "$sha" = '*' ] || [[ "$full" == "$sha"* ]]; } && [[ " $* " == *" $pat "* ]]; then
      echo "stub docker: failing on $pat" >&2
      exit 1
    fi
  done < "$STUB_DIR/docker_fail"
fi
exit 0
"""

CURL_STUB = r"""#!/usr/bin/env bash
echo "curl $*" >> "$STUB_DIR/calls.log"
full=$(git rev-parse HEAD 2>/dev/null || echo none)
if [ -f "$STUB_DIR/unhealthy" ]; then
  while read -r sha; do
    [ -z "$sha" ] && continue
    if [ "$sha" = '*' ] || [[ "$full" == "$sha"* ]]; then
      echo "curl: (22) The requested URL returned error: 503" >&2
      exit 22
    fi
  done < "$STUB_DIR/unhealthy"
fi
echo ok
"""

FLOCK_STUB = r"""#!/usr/bin/env bash
echo "flock $*" >> "$STUB_DIR/calls.log"
[ -f "$STUB_DIR/flock_busy" ] && exit 1
exit 0
"""

ENV_PROD = f"""DOMAIN=deploy.example.test
ACME_EMAIL=ops@example.test
POSTGRES_PASSWORD={SECRET}
JWT_SECRET={SECRET}-jwt
SNAPSHOT_KEY={SECRET}-key
"""


def git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": BASE_PATH,
        "HOME": str(cwd),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


class Sandbox:
    def __init__(self, root: Path, real_flock: bool = False) -> None:
        self.root = root
        self.stub = root / "stubs"
        self.bin = root / "bin"
        self.stub.mkdir()
        self.bin.mkdir()
        stubs = {"docker": DOCKER_STUB, "curl": CURL_STUB}
        if not real_flock:
            stubs["flock"] = FLOCK_STUB
        for name, body in stubs.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        self.dev = root / "dev"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.dev))
        git(self.dev, "checkout", "-q", "-b", "main")
        (self.dev / "deploy").mkdir()
        shutil.copy(DEPLOY_SH, self.dev / "deploy" / "deploy.sh")
        (self.dev / "deploy" / "deploy.sh").chmod(0o755)
        (self.dev / ".gitignore").write_text(
            "deploy/.env.prod\ndeploy/.last_good\ndeploy/.deploy.lock\n"
        )
        (self.dev / "docker-compose.yml").write_text("services: {}\n")
        (self.dev / "docker-compose.prod.yml").write_text("services: {}\n")
        self.base = self.push({"migrations/versions/0001_a.py": "# 0001\n"}, "base")
        git(root, "clone", "-q", str(self.origin), str(self.repo))
        (self.repo / "deploy" / ".env.prod").write_text(ENV_PROD)
        (self.repo / "deploy" / ".env.prod").chmod(0o600)

    def push(self, files: dict[str, str], msg: str) -> str:
        for rel, body in files.items():
            p = self.dev / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", msg)
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        return git(self.dev, "rev-parse", "HEAD")

    def set_file(self, name: str, content: str) -> None:
        (self.stub / name).write_text(content)

    @property
    def last_good(self) -> Path:
        return self.repo / "deploy" / ".last_good"

    def calls(self) -> list[str]:
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD")

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{BASE_PATH}",
            "HOME": str(self.root),
            "STUB_DIR": str(self.stub),
            "HEALTH_TIMEOUT": "1",
            "HEALTH_INTERVAL": "0",
        }
        env.update(extra)
        return env

    def popen(self, *args: str, **extra: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["bash", "deploy/deploy.sh", *args],
            cwd=self.repo,
            env=self.env(**extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "deploy/deploy.sh", *args],
            cwd=self.repo,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=60,
        )


@pytest.fixture
def sb(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def out(r: subprocess.CompletedProcess[str]) -> str:
    return r.stdout + r.stderr


def index(calls: list[str], needle: str) -> int:
    return next(i for i, c in enumerate(calls) if needle in c)


def test_script_exists_and_parses() -> None:
    assert DEPLOY_SH.exists()
    subprocess.run(["bash", "-n", str(DEPLOY_SH)], check=True)
    text = DEPLOY_SH.read_text()
    assert "set -euo pipefail" in text
    assert not re.search(r"set\s+-[a-z]*x", text), "set -x would leak values"
    assert "system prune" not in text and "prune -a" not in text and "--all" not in text


def test_healthy_deploy_updates_last_good(sb: Sandbox) -> None:
    new = sb.push({"app.txt": "v2\n"}, "v2")
    r = sb.run(new)
    assert r.returncode == 0, out(r)
    assert sb.last_good.read_text().strip() == new
    assert sb.head() == new
    calls = sb.calls()
    compose = (
        "docker compose -f docker-compose.yml -f docker-compose.prod.yml "
        "--env-file deploy/.env.prod -p home-awareness"
    )
    docker_calls = [c for c in calls if c.startswith("docker ")]
    assert all(
        c.startswith(compose) or "image prune" in c or "compose version" in c for c in docker_calls
    ), docker_calls
    assert index(calls, " build") < index(calls, "run --rm migrate") < index(calls, " up -d --wait")
    assert "docker image prune -f" in " ".join(calls)
    assert not any("system prune" in c or "--all" in c or "prune -a" in c for c in calls)
    health = [c for c in calls if c.startswith("curl")]
    assert health and "https://deploy.example.test/health" in health[0]
    assert "--max-time 5" in health[0] and "-fsS" in health[0]
    assert " -k" not in health[0] and "--insecure" not in health[0]


def test_default_ref_is_origin_main(sb: Sandbox) -> None:
    new = sb.push({"app.txt": "v2\n"}, "v2")
    r = sb.run()
    assert r.returncode == 0, out(r)
    assert sb.head() == new


def test_health_url_override_and_curl_extra(sb: Sandbox) -> None:
    new = sb.push({"app.txt": "v2\n"}, "v2")
    r = sb.run(new, HEALTH_URL="https://localhost:8443/health", CURL_EXTRA="-k")
    assert r.returncode == 0, out(r)
    health = next(c for c in sb.calls() if c.startswith("curl"))
    assert "https://localhost:8443/health" in health and "-k" in health


def test_build_failure_changes_nothing(sb: Sandbox) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("docker_fail", f"{new} build\n")
    r = sb.run(new)
    assert r.returncode != 0
    assert sb.last_good.read_text().strip() == sb.base
    assert sb.head() == sb.base, "working tree must be restored"
    calls = " ".join(sb.calls())
    assert "run --rm migrate" not in calls and " up " not in calls
    assert "stop" not in calls and " down" not in calls


def test_migration_failure_stops_before_switch(sb: Sandbox) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"migrations/versions/0002_b.py": "# 0002\n"}, "v2")
    sb.set_file("docker_fail", f"{new} migrate\n")
    r = sb.run(new)
    assert r.returncode != 0
    calls = " ".join(sb.calls())
    assert " up " not in calls and "stop" not in calls
    assert sb.last_good.read_text().strip() == sb.base
    assert sb.head() == sb.base
    assert "migrat" in out(r).lower()


def test_unhealthy_new_version_rolls_back(sb: Sandbox) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("unhealthy", new + "\n")
    r = sb.run(new)
    assert r.returncode != 0, "a rolled back deploy is still a failed deploy"
    assert f"ROLLED BACK to {sb.base}" in out(r)
    assert sb.head() == sb.base
    assert sb.last_good.read_text().strip() == sb.base
    calls = sb.calls()
    ups = [c for c in calls if " up -d" in c]
    assert len(ups) == 2 and ups[1].endswith(f"@{sb.base[:7]}")
    assert "--no-deps" in ups[1] and f"api worker beat @{sb.base[:7]}" in ups[1]
    assert " caddy" not in ups[1] and " db" not in ups[1]
    assert len([c for c in calls if "run --rm migrate" in c]) == 1, "rollback must not migrate"
    rebuilds = [c for c in calls if c.endswith(f"build api worker beat @{sb.base[:7]}")]
    assert rebuilds, calls
    # health was checked again after the rollback (the old version is also verified)
    assert any(c.startswith("curl") for c in calls[calls.index(ups[1]) :])


def test_rollback_that_is_unhealthy_reports_failure(sb: Sandbox) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("unhealthy", "*\n")
    r = sb.run(new)
    assert r.returncode != 0
    assert "ROLLBACK FAILED, manual action needed" in out(r)
    assert "checked out" in out(r) and sb.base in out(r)
    assert any("ps --format" in c for c in sb.calls())
    assert "ROLLED BACK" not in out(r)
    assert sb.last_good.read_text().strip() == sb.base


def test_failed_up_after_switch_rolls_back(sb: Sandbox) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("docker_fail", f"{new} up\n")
    r = sb.run(new)
    assert r.returncode != 0
    assert f"ROLLED BACK to {sb.base}" in out(r)


def test_first_deploy_failure_does_not_roll_back(sb: Sandbox) -> None:
    assert not sb.last_good.exists()
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("unhealthy", "*\n")
    r = sb.run(new)
    assert r.returncode != 0
    assert "ROLLED BACK" not in out(r)
    assert "first deploy" in out(r).lower()
    calls = sb.calls()
    assert len([c for c in calls if " up -d" in c]) == 1
    stops = [c for c in calls if " stop" in c]
    assert stops and all(
        c.split("stop", 1)[1].split("@")[0].split() == ["api", "worker", "beat"] for c in stops
    )
    assert not sb.last_good.exists()


def test_first_automatic_deploy_after_manual_bootstrap_rolls_back(sb: Sandbox) -> None:
    """No .last_good, but the stack was healthy before the switch: that is the rollback target."""
    assert not sb.last_good.exists()
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("unhealthy", new + "\n")  # the old (checked-out) version is healthy
    r = sb.run(new)
    assert r.returncode != 0
    assert f"ROLLED BACK to {sb.base}" in out(r)
    assert sb.head() == sb.base
    assert not any(" stop" in c for c in sb.calls()), "a healthy production must not be stopped"


def test_rollback_warns_about_forward_only_migrations(sb: Sandbox) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"migrations/versions/0002_b.py": "# 0002\n"}, "v2")
    sb.set_file("unhealthy", new + "\n")
    r = sb.run(new)
    assert r.returncode != 0
    assert "forward-only" in out(r)
    assert "0002_b.py" in out(r)


def test_no_migration_warning_when_migrations_unchanged(sb: Sandbox) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("unhealthy", new + "\n")
    assert "forward-only" not in out(sb.run(new))


def test_lock_busy_refuses_immediately(sb: Sandbox) -> None:
    sb.set_file("flock_busy", "")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    r = sb.run(new)
    assert r.returncode != 0
    assert "another deploy" in out(r).lower()
    assert not [c for c in sb.calls() if c.startswith("docker")]
    assert sb.head() == sb.base


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock not installed")
def test_two_concurrent_runs_second_is_refused(tmp_path: Path) -> None:
    sb = Sandbox(tmp_path, real_flock=True)
    sb.env()  # no flock stub: the real one is found in /usr/bin
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("slow", "")
    first = sb.popen(new)
    deadline = time.time() + 10
    while time.time() < deadline and not any(" build" in c for c in sb.calls()):
        time.sleep(0.05)
    started = time.time()
    second = sb.run(new)
    assert time.time() - started < 1.5, "second run must not wait for the first"
    assert second.returncode != 0
    assert "another deploy" in out(second).lower()
    stdout, stderr = first.communicate(timeout=30)
    assert first.returncode == 0, stdout + stderr
    assert sb.last_good.read_text().strip() == new


@pytest.mark.parametrize(
    "bad",
    [
        "--help",
        "-x",
        "--upload-pack=touch /tmp/pwned",
        "; rm -rf /",
        "main; ls",
        "$(touch pwned)",
        "`id`",
        "a b",
        "../etc",
        "a..b",
        "feature/",
        "-",
        "",
        "origin/main\nrm",
    ],
)
def test_bad_ref_is_rejected_before_anything_runs(sb: Sandbox, bad: str) -> None:
    r = sb.run(bad)
    assert r.returncode != 0
    assert "invalid" in out(r).lower()
    assert sb.calls() == []
    assert sb.head() == sb.base
    assert not (sb.repo / "pwned").exists()


@pytest.mark.parametrize("good", ["origin/main", "main", "feature/new-thing_1.2"])
def test_valid_ref_shapes_are_accepted(sb: Sandbox, good: str) -> None:
    git(sb.dev, "push", "-q", "origin", "HEAD:refs/heads/feature/new-thing_1.2")
    r = sb.run(good)
    assert r.returncode == 0, out(r)


def test_older_sha_after_newer_is_a_green_noop(sb: Sandbox) -> None:
    new = sb.push({"app.txt": "v2\n"}, "v2")
    assert sb.run(new).returncode == 0
    sb.set_file("calls.log", "")
    r = sb.run(sb.base)
    assert r.returncode == 0, out(r)
    assert "already at a newer commit, nothing to do" in out(r)
    assert sb.head() == new
    assert not any(" build" in c for c in sb.calls())


def test_refuses_non_fast_forward(sb: Sandbox) -> None:
    new = sb.push({"app.txt": "v2\n"}, "v2")
    git(sb.dev, "checkout", "-q", "-b", "alt", sb.base)
    (sb.dev / "other.txt").write_text("x\n")
    git(sb.dev, "add", "-A")
    git(sb.dev, "commit", "-q", "-m", "diverged")
    alt = git(sb.dev, "rev-parse", "HEAD")
    git(sb.dev, "push", "-q", "origin", "HEAD:refs/heads/alt")
    assert sb.run(new).returncode == 0
    r = sb.run(alt)
    assert r.returncode != 0
    assert "fast-forward" in out(r).lower()
    assert sb.head() == new
    # an older commit while HEAD is not the last good one is also refused, not a no-op
    sb.last_good.write_text(sb.base + "\n")
    assert sb.run(sb.base).returncode != 0


def test_refuses_dirty_tracked_files(sb: Sandbox) -> None:
    new = sb.push({"app.txt": "v2\n"}, "v2")
    (sb.repo / "docker-compose.yml").write_text("services: {a: {}}\n")
    r = sb.run(new)
    assert r.returncode != 0
    assert "local modifications" in out(r)
    assert not any(" build" in c or " up " in c for c in sb.calls())
    assert sb.head() == sb.base


def test_fetch_failure_is_reported(sb: Sandbox) -> None:
    git(sb.repo, "remote", "set-url", "origin", str(sb.root / "does-not-exist.git"))
    r = sb.run(sb.base)
    assert r.returncode != 0
    assert "deploy:" in r.stderr and "fetch" in r.stderr.lower()


def test_old_compose_is_refused_early(sb: Sandbox) -> None:
    sb.set_file("compose_version", "v2.20.1\n")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    r = sb.run(new)
    assert r.returncode != 0
    assert "2.24" in out(r)
    assert not any(" build" in c for c in sb.calls())


def test_build_and_migrate_failures_warn_about_image_tags(sb: Sandbox) -> None:
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("docker_fail", f"{new} build\n")
    assert "image tags" in out(sb.run(new))


def test_health_check_resolves_domain_locally_by_default(sb: Sandbox) -> None:
    new = sb.push({"app.txt": "v2\n"}, "v2")
    assert sb.run(new).returncode == 0
    health = next(c for c in sb.calls() if c.startswith("curl"))
    assert "--resolve deploy.example.test:443:127.0.0.1" in health
    assert "https://deploy.example.test/health" in health


def test_survives_a_closed_stdout(sb: Sandbox) -> None:
    """An SSH drop must not kill the script mid-switch (SIGPIPE/SIGHUP are ignored)."""
    new = sb.push({"app.txt": "v2\n"}, "v2")
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    proc = subprocess.run(
        ["bash", "deploy/deploy.sh", new],
        cwd=sb.repo,
        env=sb.env(),
        stdout=write_fd,
        stderr=write_fd,
        timeout=60,
    )
    os.close(write_fd)
    assert proc.returncode == 0
    assert sb.last_good.read_text().strip() == new


def test_survives_a_closed_stdout_during_rollback(sb: Sandbox) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    sb.set_file("unhealthy", new + "\n")
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    proc = subprocess.run(
        ["bash", "deploy/deploy.sh", new],
        cwd=sb.repo,
        env=sb.env(),
        stdout=write_fd,
        stderr=write_fd,
        timeout=60,
    )
    os.close(write_fd)
    assert proc.returncode == 1
    assert sb.head() == sb.base
    assert any(c.startswith("docker") and "--no-deps" in c for c in sb.calls())


def test_gitignore_covers_runtime_files() -> None:
    for name in (
        "deploy/.last_good",
        "deploy/.last_good.tmp",
        "deploy/.deploy.lock",
        "deploy/last-deploy.log",
    ):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT)
        assert r.returncode == 0, name


def test_missing_env_file_fails_clearly(sb: Sandbox) -> None:
    (sb.repo / "deploy" / ".env.prod").unlink()
    r = sb.run(sb.base)
    assert r.returncode != 0
    assert ".env.prod" in out(r)
    assert not [c for c in sb.calls() if c.startswith("docker")]


def test_missing_domain_fails_clearly(sb: Sandbox) -> None:
    (sb.repo / "deploy" / ".env.prod").write_text("POSTGRES_PASSWORD=x\n")
    r = sb.run(sb.base)
    assert r.returncode != 0
    assert "DOMAIN" in out(r)


@pytest.mark.parametrize("scenario", ["healthy", "unhealthy", "build_fail", "bad_ref"])
def test_no_secret_value_in_output(sb: Sandbox, scenario: str) -> None:
    sb.last_good.write_text(sb.base + "\n")
    new = sb.push({"app.txt": "v2\n"}, "v2")
    ref = new
    if scenario == "unhealthy":
        sb.set_file("unhealthy", "*\n")
    elif scenario == "build_fail":
        sb.set_file("docker_fail", "* build\n")
    elif scenario == "bad_ref":
        ref = "; cat deploy/.env.prod"
    r = sb.run(ref)
    assert SECRET not in r.stdout and SECRET not in r.stderr
    assert "POSTGRES_PASSWORD" not in out(r)
    assert SECRET not in " ".join(sb.calls())


# --- workflow ---------------------------------------------------------------------------


def _workflow_text() -> str:
    assert WORKFLOW.exists()
    return WORKFLOW.read_text()


def _workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(_workflow_text())


def test_workflow_triggers_and_permissions() -> None:
    wf = _workflow()
    on = wf.get("on", wf.get(True))
    assert on["workflow_run"]["workflows"] == ["ci"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert on["workflow_run"]["branches"] == ["main"]
    assert "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["group"] == "deploy"
    assert wf["concurrency"]["cancel-in-progress"] is False
    for job in wf["jobs"].values():
        assert "permissions" not in job or job["permissions"] == {"contents": "read"}


def test_workflow_only_runs_for_verified_main() -> None:
    wf = _workflow()
    cond = next(iter(wf["jobs"].values()))["if"]
    assert "workflow_run.conclusion == 'success'" in cond
    assert "workflow_run.head_branch == 'main'" in cond
    assert "refs/heads/main" in cond  # manual runs only from main
    assert "head_repository.full_name == github.repository" in cond


def test_workflow_uses_ci_verified_sha_via_env() -> None:
    text = _workflow_text()
    assert "github.event.workflow_run.head_sha" in text
    # untrusted values must reach the shell through env, never interpolated into run: blocks
    for block in re.findall(r"run: \|(.*?)(?=\n\s{0,8}- |\Z)", text, flags=re.S):
        assert "${{" not in block, block
    assert re.search(r"\[0-9a-f\]\{40\}", text), "sha is validated"


def test_workflow_secret_guard_uses_env_not_secrets_in_if() -> None:
    wf = _workflow()
    text = _workflow_text()
    for line in text.splitlines():
        if re.match(r"\s*if:", line):
            assert "secrets." not in line
    steps = next(iter(wf["jobs"].values()))["steps"]
    guard = steps[0]
    assert guard["env"]["DEPLOY_HOST"] == "${{ secrets.DEPLOY_HOST }}"
    assert "::notice" in guard["run"] and "GITHUB_OUTPUT" in guard["run"]
    assert all("steps." in s["if"] for s in steps[1:] if "if" in s)
    assert any("if" in s for s in steps[1:])


def test_workflow_ssh_is_pinned_and_strict() -> None:
    text = _workflow_text()
    assert "secrets.DEPLOY_HOST_KEY" in text and "secrets.DEPLOY_SSH_KEY" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "UserKnownHostsFile=" in text
    assert "BatchMode=yes" in text
    assert "StrictHostKeyChecking=no" not in text and "accept-new" not in text
    assert "ssh-keyscan" not in text
    assert "chmod 600" in text and "trap" in text and "rm -f" in text
    assert "set -x" not in text and 'echo "$DEPLOY_SSH_KEY' not in text
    assert "./deploy/deploy.sh" in text


def test_workflow_detaches_the_remote_run_from_the_ssh_session() -> None:
    text = _workflow_text()
    assert "setsid -w nohup ./deploy/deploy.sh" in text
    assert ">deploy/last-deploy.log 2>&1 </dev/null" in text
    assert "cat deploy/last-deploy.log" in text
    assert "\\$HOME" in text, "a leading ~/ in DEPLOY_PATH expands on the remote side"
    assert "workflow_dispatch" in text and "not necessarily CI-green" in text
    assert "one pending" in text


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    subprocess.run(["shellcheck", str(DEPLOY_SH)], check=True)
