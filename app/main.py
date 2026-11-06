from typing import Annotated

from fastapi import Depends, FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.router import router as auth_router
from app.db import get_session
from app.logging import configure_logging
from app.redis import get_redis

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
    return app


app = create_app()
