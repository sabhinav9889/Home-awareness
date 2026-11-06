import asyncio
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.router import router as alerts_router
from app.auth.router import router as auth_router
from app.config import get_settings
from app.db import _sessionmaker, get_session
from app.devices.offline import offline_loop
from app.devices.router import router as devices_router
from app.disclaimer import DISCLAIMER
from app.events.router import router as events_router
from app.households.router import router as households_router
from app.live.router import router as live_router
from app.logging import RequestLogMiddleware, configure_logging
from app.metrics import render
from app.redis import get_redis
from app.rules.router import router as rules_router


def create_app(*, run_background_jobs: bool = False) -> FastAPI:
    """Background jobs are opt-in so tests never start a real loop; the entrypoint enables them."""
    configure_logging()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if run_background_jobs and get_settings().job_runner == "inprocess":
            task = asyncio.create_task(offline_loop(_sessionmaker(), await get_redis()))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Home Awareness", description=DISCLAIMER, lifespan=lifespan)

    @app.get("/health", description=DISCLAIMER)
    async def health(
        session: Annotated[AsyncSession, Depends(get_session)],
        redis: Annotated[Redis, Depends(get_redis)],
    ) -> dict[str, str]:
        await session.execute(text("SELECT 1"))
        await redis.ping()
        return {"status": "ok", "db": "ok", "redis": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        body, content_type = render()
        return Response(body, media_type=content_type)

    app.add_middleware(RequestLogMiddleware)
    app.include_router(auth_router)
    app.include_router(households_router)
    app.include_router(rules_router)
    app.include_router(devices_router)
    app.include_router(events_router)
    app.include_router(alerts_router)
    app.include_router(live_router)
    return app


app = create_app(run_background_jobs=True)
