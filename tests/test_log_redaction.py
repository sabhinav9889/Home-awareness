"""Bot-token redaction, checked in fresh interpreters: in-process checks can't fail once any
earlier test has installed the (process-global, idempotent) record factory."""

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123:SECRET-TOKEN_x"
ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:1/none",
    "REDIS_URL": "redis://localhost:1/0",
    "JWT_SECRET": "subprocess-only-jwt-secret-0123456789abcdef",
    "PATH": "/usr/bin:/bin",
}


def _run(setup: str, tmp_path) -> str:
    code = textwrap.dedent(
        f"""
        import logging, sys
        sys.path.insert(0, {str(ROOT)!r})
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(name)s %(message)s")
        {setup}
        logging.getLogger("httpx").setLevel(logging.INFO)  # even if something re-enables it
        url = "https://api.telegram.org/bot{TOKEN}/sendMessage"
        logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"', url)
        logging.getLogger("app.anything").info("calling %s", url)
        """
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,  # no .env here: the child sees only ENV
        env=ENV,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_celery_entrypoint_never_logs_bot_token(tmp_path):
    out = _run("import app.alerts.celery_app", tmp_path)
    assert TOKEN not in out
    assert out.count("bot***") == 2, out  # both records were emitted, redacted


def test_api_configure_logging_never_logs_bot_token(tmp_path):
    out = _run("from app.logging import configure_logging; configure_logging()", tmp_path)
    assert TOKEN not in out
    assert out.count("bot***") == 2, out
