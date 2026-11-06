import os

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.main as app_main  # imports every router/model before create_all
from app.db import Base, get_session
from app.redis import get_redis

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://home:home@localhost:5432/home_test"
)

_tables_created = False


async def _ensure_tables() -> None:
    global _tables_created
    if _tables_created:
        return
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
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
