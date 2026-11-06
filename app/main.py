from typing import Annotated

from fastapi import Depends, FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.router import router as auth_router
from app.db import get_session
from app.devices.router import router as devices_router
from app.events.router import router as events_router
from app.households.router import router as households_router
from app.logging import configure_logging
from app.redis import get_redis
from app.rules.router import router as rules_router

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Home Awareness", description=DISCLAIMER)

    @app.get("/health", description=DISCLAIMER)
    async def health(
        session: Annotated[AsyncSession, Depends(get_session)],
        redis: Annotated[Redis, Depends(get_redis)],
    ) -> dict[str, str]:
        await session.execute(text("SELECT 1"))
        await redis.ping()
        return {"status": "ok", "db": "ok", "redis": "ok"}

    app.include_router(auth_router)
    app.include_router(households_router)
    app.include_router(rules_router)
    app.include_router(devices_router)
    app.include_router(events_router)
    return app


app = create_app()
