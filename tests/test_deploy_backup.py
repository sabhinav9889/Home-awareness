"""Backup and restore script tests. pg_dump, psql and df are stubs on PATH; gzip is the real one.

The real-Postgres drill at the bottom needs a Docker daemon: it starts a throwaway compose project
(`ha-deploy-test`, no host ports) and removes it again; it never touches any other container.
"""

import gzip
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKUP_SH = ROOT / "deploy" / "backup.sh"
RESTORE_SH = ROOT / "deploy" / "restore.sh"
SECRET = "s3cr3t-VALUE-must-never-print-9f8e7d"
HAVE_DOCKER = shutil.which("docker") is not None
needs_docker = pytest.mark.skipif(not HAVE_DOCKER, reason="docker not available")

PG_DUMP_STUB = r"""#!/usr/bin/env bash
echo "pg_dump $*" >> "$STUB_DIR/calls.log"
if [ -f "$STUB_DIR/dump_nothing" ]; then echo "-- empty"; exit 0; fi
echo "-- dump"
echo "CREATE TABLE public.a (id int);"
echo "CREATE TABLE public.b (id int);"
if [ -f "$STUB_DIR/dump_fail" ]; then
  echo "pg_dump: error: connection to server failed" >&2
  exit 1
fi
exit 0
"""

PSQL_STUB = r"""#!/usr/bin/env bash
echo "psql $*" >> "$STUB_DIR/calls.log"
args="$*"
if [[ "$args" == *"-f -"* ]]; then
  cat > "$STUB_DIR/loaded.sql"; touch "$STUB_DIR/loaded"; exit 0
fi
q="${args,,}"
if [[ "$q" == *pg_database* ]]; then cat "$STUB_DIR/db_exists" 2>/dev/null; exit 0; fi
if [[ "$q" == *"count(*) from information_schema"* ]]; then
  if [ -f "$STUB_DIR/loaded" ]; then f=tables_after; else f=tables_before; fi
  cat "$STUB_DIR/$f"
  exit 0
fi
if [[ "$q" == *information_schema.tables* ]]; then printf 'a\nb\n'; exit 0; fi
if [[ "$q" == *"count(*) from"* ]]; then echo 7; exit 0; fi
exit 0
"""

DF_STUB = r"""#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "stub 99999999 1 ${STUB_DF_KB:-99999999} 1% /"
"""


@pytest.fixture
def env(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    for name, body in (("pg_dump", PG_DUMP_STUB), ("psql", PSQL_STUB), ("df", DF_STUB)):
        (stub / name).write_text(body)
        (stub / name).chmod(0o755)
    backups = tmp_path / "backups"
    backups.mkdir()
    (stub / "tables_before").write_text("0\n")
    (stub / "tables_after").write_text("2\n")
    return {
        "PATH": f"{stub}:/usr/bin:/bin",
        "STUB_DIR": str(stub),
        "BACKUP_DIR": str(backups),
        "PGPASSWORD": SECRET,
        "PGHOST": "db",
        "PGUSER": "home",
        "PGDATABASE": "home",
        "HOME": str(tmp_path),
    }


def _run(script: Path, env: dict, *args: str, extra: dict | None = None):
    return subprocess.run(
        ["bash", str(script), *args], env={**env, **(extra or {})},
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def _calls(env) -> str:
    p = Path(env["STUB_DIR"]) / "calls.log"
    return p.read_text() if p.exists() else ""


def _files(env) -> list[str]:
    return sorted(p.name for p in Path(env["BACKUP_DIR"]).iterdir())


def _make(env, stamp: str) -> str:
    name = f"home-{stamp}.sql.gz"
    with gzip.open(Path(env["BACKUP_DIR"]) / name, "wt") as f:
        f.write("CREATE TABLE t (id int);\n")
    return name


def test_scripts_exist_and_parse():
    for script in (BACKUP_SH, RESTORE_SH):
        assert script.is_file(), script
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
        text = script.read_text()
        assert "set -euo pipefail" in text


def test_shellcheck_if_installed():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run(
        ["shellcheck", str(BACKUP_SH), str(RESTORE_SH)], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout


# ---------------------------------------------------------------- backup.sh


def test_backup_writes_verified_gzip_with_expected_name(env):
    r = _run(BACKUP_SH, env)
    assert r.returncode == 0, r.stderr
    (name,) = _files(env)
    assert re.fullmatch(r"home-\d{8}-\d{6}\.sql\.gz", name)
    text = gzip.decompress((Path(env["BACKUP_DIR"]) / name).read_bytes()).decode()
    assert "CREATE TABLE" in text
    calls = _calls(env)
    assert "--no-owner" in calls and "--no-privileges" in calls


def test_retention_keeps_14_daily_and_8_weekly(env):
    today = datetime.now().date()
    base = f"{today:%Y%m%d}"
    names = {}
    for ago in range(1, 91):
        d = today - timedelta(days=ago)
        names[d] = _make(env, f"{d:%Y%m%d}-033000")
    # a second, older file on the same day must be pruned, the later one kept
    d3 = today - timedelta(days=3)
    dup = _make(env, f"{d3:%Y%m%d}-010000")
    # files that are not ours are never touched
    (Path(env["BACKUP_DIR"]) / "notes.txt").write_text("x")
    (Path(env["BACKUP_DIR"]) / "home-foo.sql.gz").write_text("x")

    r = _run(BACKUP_SH, env)
    assert r.returncode == 0, r.stderr

    daily = {today - timedelta(days=n) for n in range(1, 14)}
    older_sundays = sorted((d for d in names if d not in daily and d.weekday() == 6), reverse=True)[
        :8
    ]
    expected = {names[d] for d in daily | set(older_sundays)}
    present = {f for f in _files(env) if re.fullmatch(r"home-\d{8}-\d{6}\.sql\.gz", f)}
    today_files = {f for f in present if f.startswith(f"home-{base}-")}
    assert len(today_files) == 1
    assert present - today_files == expected
    assert len(expected) == 13 + 8
    assert dup not in present
    assert {"notes.txt", "home-foo.sql.gz"} <= set(_files(env))


def test_failed_dump_keeps_old_backups_and_leaves_no_part(env):
    old = [_make(env, "20240101-033000"), _make(env, "20240102-033000")]
    (Path(env["STUB_DIR"]) / "dump_fail").touch()
    r = _run(BACKUP_SH, env)
    assert r.returncode != 0
    assert _files(env) == old
    assert "backup failed" in (r.stdout + r.stderr).lower()


def test_dump_without_tables_is_rejected(env):
    old = _make(env, "20240101-033000")
    (Path(env["STUB_DIR"]) / "dump_nothing").touch()
    r = _run(BACKUP_SH, env)
    assert r.returncode != 0
    assert _files(env) == [old]


def test_low_disk_refuses_before_dumping(env):
    old = _make(env, "20240101-033000")
    r = _run(BACKUP_SH, env, extra={"STUB_DF_KB": "100000"})  # ~97 MB free, default needs 1 GB
    assert r.returncode != 0
    assert "free space" in (r.stdout + r.stderr).lower()
    assert "pg_dump" not in _calls(env)
    assert _files(env) == [old]
    ok = _run(BACKUP_SH, env, extra={"STUB_DF_KB": "100000", "MIN_FREE_MB": "50"})
    assert ok.returncode == 0, ok.stderr


def test_backup_never_prints_credentials(env):
    for fail in (False, True):
        flag = Path(env["STUB_DIR"]) / "dump_fail"
        flag.touch() if fail else flag.unlink(missing_ok=True)
        r = _run(BACKUP_SH, env)
        assert SECRET not in r.stdout + r.stderr
    assert SECRET not in _calls(env)


def _rclone_stub(env, exit_code: int):
    stub = Path(env["STUB_DIR"]) / "rclone"
    stub.write_text(
        f'#!/usr/bin/env bash\necho "rclone $*" >> "$STUB_DIR/calls.log"\nexit {exit_code}\n'
    )
    stub.chmod(0o755)


def test_rclone_copy_runs_after_local_write(env):
    _rclone_stub(env, 0)
    r = _run(BACKUP_SH, env, extra={"RCLONE_REMOTE": "oci:bucket/home"})
    assert r.returncode == 0, r.stderr
    (name,) = _files(env)
    assert f"rclone copy {env['BACKUP_DIR']}/{name} oci:bucket/home" in _calls(env)


def test_rclone_failure_exits_3_and_keeps_everything(env):
    _rclone_stub(env, 1)
    old = _make(env, "20240101-033000")
    r = _run(BACKUP_SH, env, extra={"RCLONE_REMOTE": "oci:bucket/home"})
    assert r.returncode == 3
    assert "warning" in (r.stdout + r.stderr).lower()
    assert old in _files(env) and len(_files(env)) == 2


def test_no_rclone_call_without_remote(env):
    _rclone_stub(env, 0)
    assert _run(BACKUP_SH, env).returncode == 0
    assert "rclone" not in _calls(env)


def _daemon(env, seconds: float, **extra):
    p = subprocess.Popen(
        ["bash", str(BACKUP_SH), "--daemon"],
        env={**env, "POLL_SECONDS": "1", **extra}, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    time.sleep(seconds)
    p.terminate()
    out, _ = p.communicate(timeout=20)
    return out


def test_daemon_catches_up_once_a_day(env):
    out = _daemon(env, 5, BACKUP_AT="0000")
    assert len(_files(env)) == 1, out
    assert _calls(env).count("pg_dump") == 1


def test_daemon_skips_when_today_already_has_a_backup(env):
    _make(env, f"{datetime.now():%Y%m%d}-000100")
    _daemon(env, 3, BACKUP_AT="0000")
    assert "pg_dump" not in _calls(env)


def test_daemon_retries_failure_only_after_retry_interval(env):
    (Path(env["STUB_DIR"]) / "dump_fail").touch()
    _daemon(env, 5, BACKUP_AT="0000", RETRY_SECONDS="3600")
    assert _calls(env).count("pg_dump") == 1
    assert _files(env) == []


# --------------------------------------------------------------- restore.sh


def _backup_file(env) -> str:
    name = _make(env, "20240101-033000")
    return str(Path(env["BACKUP_DIR"]) / name)


def _wrote(env) -> bool:
    return (Path(env["STUB_DIR"]) / "loaded").exists()


def test_restore_into_new_default_database(env):
    f = _backup_file(env)
    r = _run(RESTORE_SH, env, f)
    assert r.returncode == 0, r.stderr
    assert "home_restore" in _calls(env)
    assert "create database" in _calls(env).lower()
    assert _wrote(env)
    assert "tables: 2" in r.stdout.lower()
    assert "a: 7" in r.stdout


def test_restore_refuses_live_database_without_flag(env):
    f = _backup_file(env)
    r = _run(RESTORE_SH, env, f, "--into", "home")
    assert r.returncode != 0
    assert "--yes-overwrite" in r.stdout + r.stderr
    assert not _wrote(env)
    assert "drop schema" not in _calls(env).lower()


def test_restore_into_live_database_with_both_flags(env):
    f = _backup_file(env)
    (Path(env["STUB_DIR"]) / "db_exists").write_text("1\n")
    (Path(env["STUB_DIR"]) / "tables_before").write_text("5\n")
    r = _run(RESTORE_SH, env, f, "--into", "home", "--yes-overwrite")
    assert r.returncode == 0, r.stderr
    assert _wrote(env)
    assert "drop schema" in _calls(env).lower()


def test_restore_refuses_non_empty_target(env):
    f = _backup_file(env)
    (Path(env["STUB_DIR"]) / "db_exists").write_text("1\n")
    (Path(env["STUB_DIR"]) / "tables_before").write_text("5\n")
    r = _run(RESTORE_SH, env, f)
    assert r.returncode != 0
    assert "--yes-overwrite" in r.stdout + r.stderr
    assert not _wrote(env)


def test_restore_rejects_corrupt_and_missing_files(env):
    bad = Path(env["BACKUP_DIR"]) / "home-20240101-033000.sql.gz"
    bad.write_text("not gzip")
    assert _run(RESTORE_SH, env, str(bad)).returncode != 0
    assert _run(RESTORE_SH, env, str(bad) + ".nope").returncode != 0
    assert not _wrote(env)
    assert "psql" not in _calls(env)


def test_restore_rejects_unsafe_database_names_and_bad_usage(env):
    f = _backup_file(env)
    assert _run(RESTORE_SH, env, f, "--into", 'x"; drop database home; --').returncode != 0
    assert _run(RESTORE_SH, env).returncode != 0
    assert _run(RESTORE_SH, env, f, "--bogus").returncode != 0
    assert not _wrote(env)


def test_restore_fails_when_nothing_was_restored(env):
    f = _backup_file(env)
    (Path(env["STUB_DIR"]) / "tables_after").write_text("0\n")
    assert _run(RESTORE_SH, env, f).returncode != 0


def test_restore_never_prints_credentials(env):
    f = _backup_file(env)
    for args in ((f,), (f, "--into", "home"), (f + ".nope",)):
        r = _run(RESTORE_SH, env, *args)
        assert SECRET not in r.stdout + r.stderr
    assert SECRET not in _calls(env)


# ------------------------------------------------------------ real drill

DRILL_ENV = {
    "DOMAIN": "localhost",
    "ACME_EMAIL": "ops@example.com",
    "POSTGRES_PASSWORD": "drill-pw-not-secret",
    "JWT_SECRET": "j" * 40,
    "SNAPSHOT_KEY": "k" * 44,
}
PROJECT = "ha-deploy-test"
COMPOSE = [
    "docker", "compose", "--env-file", "/dev/null", "-f", "docker-compose.yml",
    "-f", "docker-compose.prod.yml", "-p", PROJECT,
]  # fmt: skip


def _docker_up() -> bool:
    if not HAVE_DOCKER:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


def _dc(*args: str, check=True, stdin: str | None = None, timeout=240):
    env = {k: v for k, v in os.environ.items() if k not in DRILL_ENV}
    env.update(DRILL_ENV)
    r = subprocess.run(
        [*COMPOSE, *args], cwd=ROOT, env=env, capture_output=True, text=True, input=stdin,
        timeout=timeout,
    )  # fmt: skip
    if check:
        assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r


@pytest.mark.skipif(not _docker_up(), reason="docker daemon not reachable")
def test_real_postgres_backup_and_restore_drill():
    """DRILL: throwaway Postgres in compose project ha-deploy-test, backup.sh then restore.sh."""
    try:
        _dc("up", "-d", "--wait", "db")
        sql = """
        CREATE TABLE users (id serial PRIMARY KEY, email text NOT NULL UNIQUE);
        CREATE TABLE events (id serial PRIMARY KEY, user_id int REFERENCES users(id), body jsonb);
        CREATE INDEX events_user ON events (user_id);
        INSERT INTO users (email) SELECT 'u' || g || '@example.com' FROM generate_series(1, 5) g;
        INSERT INTO events (user_id, body)
          SELECT (g % 5) + 1, jsonb_build_object('n', g) FROM generate_series(1, 40) g;
        """
        _dc("exec", "-T", "db", "psql", "-U", "home", "-d", "home", "-v", "ON_ERROR_STOP=1",
            stdin=sql)  # fmt: skip
        run = ("run", "--rm", "--no-deps", "-T", "--entrypoint", "bash", "backup")
        r = _dc(*run, "/deploy/backup.sh")
        match = re.search(r"/backups/home-\d{8}-\d{6}\.sql\.gz", r.stdout + r.stderr)
        assert match, r.stdout + r.stderr
        r = _dc(*run, "/deploy/restore.sh", match.group(0))
        assert "tables: 2" in r.stdout.lower(), r.stdout
        assert DRILL_ENV["POSTGRES_PASSWORD"] not in r.stdout + r.stderr

        def count(db: str, table: str) -> str:
            out = _dc("exec", "-T", "db", "psql", "-U", "home", "-d", db, "-Atc",
                      f"SELECT count(*) FROM {table}")  # fmt: skip
            return out.stdout.strip()

        for table, expected in (("users", "5"), ("events", "40")):
            assert count("home", table) == count("home_restore", table) == expected
        # a second restore into the now non-empty database is refused
        again = _dc(*run, "/deploy/restore.sh", match.group(0), check=False)
        assert again.returncode != 0
    finally:
        _dc("down", "-v", "--remove-orphans", check=False)
        left = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={PROJECT}", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        )  # fmt: skip
        assert left.stdout.strip() == ""


# ------------------------------------------------------------ compose service


def _merged() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in DRILL_ENV}
    env.update(DRILL_ENV)
    r = subprocess.run(
        [*COMPOSE[:-2], "-p", "ha-config-only", "config", "--format", "json"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_docker
def test_backup_service_shape():
    merged = _merged()
    svc = merged["services"]["backup"]
    assert not svc.get("ports")
    assert svc["restart"] == "unless-stopped"
    assert svc["logging"]["driver"] == "json-file"
    assert svc["image"] == "postgres:16-alpine"
    assert svc["depends_on"]["db"]["condition"] == "service_healthy"
    assert "backups" in merged["volumes"]
    mounts = {v["target"]: v for v in svc["volumes"]}
    assert mounts["/backups"]["source"] == "backups"
    for script in ("backup.sh", "restore.sh"):
        assert mounts[f"/deploy/{script}"]["read_only"] is True
    env = svc["environment"]
    assert env["PGHOST"] == "db" and env["PGUSER"] == "home" and env["PGDATABASE"] == "home"
    assert env["PGPASSWORD"] == DRILL_ENV["POSTGRES_PASSWORD"]
    assert svc["entrypoint"][-1] == "--daemon"


@needs_docker
def test_backup_service_requires_the_password_variable():
    env = {k: v for k, v in os.environ.items() if k not in DRILL_ENV}
    env.update({k: v for k, v in DRILL_ENV.items() if k != "POSTGRES_PASSWORD"})
    r = subprocess.run(
        [*COMPOSE[:-2], "-p", "ha-config-only", "config", "-q"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert r.returncode != 0 and "POSTGRES_PASSWORD" in r.stderr


def test_readme_documents_backups():
    text = (ROOT / "deploy" / "README.md").read_text()
    for word in ("restore.sh", "SNAPSHOT_KEY", "password manager", "03:30", "RCLONE_REMOTE"):
        assert word in text, word
    assert "not backed up" in text.lower()
