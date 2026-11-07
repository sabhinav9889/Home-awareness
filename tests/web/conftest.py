"""Live-server fixtures for the dashboard's browser tests.

The real app runs under uvicorn in a background thread (its own event loop) on a free port,
against the same `home_test` database as tests/conftest.py, a fakeredis server shared with
the test thread, and an in-memory outbox instead of the console email sender. Unlike the
API tests, the server commits for real, so every web test truncates the tables afterwards.
"""

import asyncio
import shutil
import socket
import threading
import time
import types

import fakeredis
import fakeredis.aioredis
import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.alerts.jobs import get_job_runner
from app.db import Base, get_session
from app.email import get_email_sender
from app.main import create_app
from app.redis import get_redis
from tests.conftest import TEST_DATABASE_URL, _ensure_tables
from tests.doubles import RecordingJobRunner

CHROME = "/usr/bin/google-chrome"


class Outbox(list):
    async def send(self, to: str, subject: str, body: str) -> None:
        self.append(types.SimpleNamespace(to=to, subject=subject, body=body))


def _run(coro) -> None:
    """asyncio.run in a helper thread: pytest-asyncio may have a loop running in this one."""
    errors = []

    def target():
        try:
            asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001  re-raised in the caller's thread
            errors.append(e)

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if errors:
        raise errors[0]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _truncate_all() -> None:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture(scope="session")
def live_server():
    _run(_ensure_tables())
    app = create_app()
    factory = async_sessionmaker(
        create_async_engine(TEST_DATABASE_URL, poolclass=NullPool), expire_on_commit=False
    )
    redis_server = fakeredis.FakeServer()
    outbox = Outbox()
    runner = RecordingJobRunner()

    async def _session():
        async with factory() as s:
            yield s

    async def _redis():
        return fakeredis.aioredis.FakeRedis(server=redis_server, decode_responses=True)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_redis] = _redis
    app.dependency_overrides[get_email_sender] = lambda: outbox
    app.dependency_overrides[get_job_runner] = lambda: runner

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.02)
    yield types.SimpleNamespace(
        base_url=f"http://127.0.0.1:{port}",
        outbox=outbox,
        redis=fakeredis.FakeRedis(server=redis_server),
        jobs=runner,
    )
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def live(live_server):
    """A clean live server per test: empty outbox, redis and tables afterwards."""
    live_server.outbox.clear()
    live_server.redis.flushall()  # rate-limit counters from earlier tests
    yield live_server
    _run(_truncate_all())


@pytest.fixture
def browser():
    # Function-scoped on purpose: sync Playwright keeps an event loop running in this thread
    # while it is open, which would break every async (pytest-asyncio) test collected after.
    if shutil.which(CHROME) is None and shutil.which("google-chrome") is None:
        pytest.skip("Google Chrome is not installed (web tests use channel='chrome')")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        yield b
        b.close()


@pytest.fixture
def new_page(browser, live):
    """Factory: a page in a fresh browser context (its own sessionStorage)."""
    contexts = []

    def _make():
        ctx = browser.new_context(base_url=live.base_url)
        ctx.set_default_timeout(5000)
        # No third-party network in tests: the system font fallback is enough.
        ctx.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        ctx.route("https://fonts.gstatic.com/**", lambda route: route.abort())
        contexts.append(ctx)
        return ctx.new_page()

    yield _make
    for ctx in contexts:
        ctx.close()


@pytest.fixture
def page(new_page):
    return new_page()
