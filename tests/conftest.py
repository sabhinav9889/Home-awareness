import os
import types

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.main as app_main  # imports every router/model before create_all
from app.alerts.jobs import RecordingJobRunner, get_job_runner
from app.db import Base, get_session
from app.email import get_email_sender
from app.redis import get_redis

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://home:home@localhost:5432/home_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-0123")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://home:home@localhost:5432/home_test"
)

_tables_created = False


def require_test_database(url: str) -> None:
    """drop_all is destructive: refuse unless the database name ends with _test."""
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to drop all tables: test database {name!r} must end with '_test' "
            "(check TEST_DATABASE_URL)."
        )


async def _ensure_tables() -> None:
    global _tables_created
    if _tables_created:
        return
    require_test_database(TEST_DATABASE_URL)
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)  # schema changes (check constraints) must apply
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    _tables_created = True


@pytest.fixture
async def session():
    await _ensure_tables()
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.connect() as conn:
        outer = await conn.begin()
        async with AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as s:
            yield s
        await outer.rollback()
    await engine.dispose()


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def client(session, redis):
    app = app_main.app

    async def _session():
        yield session

    async def _redis():
        return redis

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_redis] = _redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class _Outbox(list):
    async def send(self, to: str, subject: str, body: str) -> None:
        self.append(types.SimpleNamespace(to=to, subject=subject, body=body))


@pytest.fixture
def outbox():
    box = _Outbox()
    app_main.app.dependency_overrides[get_email_sender] = lambda: box
    yield box
    app_main.app.dependency_overrides.pop(get_email_sender, None)


@pytest.fixture
async def signed_up(client, session, outbox):
    """Factory: sign up, confirm via the outbox token, log in -> (member, headers)."""
    from sqlalchemy import select

    from app.auth.models import Member

    async def _make(email="owner@example.com", household_name="Home", password="correct-horse-1"):
        r = await client.post(
            "/auth/signup",
            json={"email": email, "password": password, "household_name": household_name},
        )
        assert r.status_code == 201, r.text
        token = outbox[-1].body.split("token=")[1].split()[0]
        assert (await client.post("/auth/confirm", json={"token": token})).status_code == 204
        r = await client.post("/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        member = (
            await session.execute(select(Member).where(Member.email == email.strip().lower()))
        ).scalar_one()
        return member, headers

    return _make


@pytest.fixture
async def two_households(signed_up):
    """Two independent households -> (headers_a, headers_b)."""
    _, headers_a = await signed_up(email="a@example.com", household_name="A")
    _, headers_b = await signed_up(email="b@example.com", household_name="B")
    return headers_a, headers_b


@pytest.fixture(autouse=True)
def jobs():
    # autouse: no test may reach a real broker; tests that care take `jobs` by name
    runner = RecordingJobRunner()
    app_main.app.dependency_overrides[get_job_runner] = lambda: runner
    yield runner
    app_main.app.dependency_overrides.pop(get_job_runner, None)


FROZEN_RATELIMIT_NOW = 1_800_000_015.0


@pytest.fixture
def frozen_ratelimit_clock(monkeypatch):
    """Pin the rate limiter to one window so limit tests never straddle a boundary."""
    monkeypatch.setattr("app.ratelimit.now", lambda: FROZEN_RATELIMIT_NOW)
    return FROZEN_RATELIMIT_NOW
