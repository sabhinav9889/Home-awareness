import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import member_from_token
from app.db import get_session
from app.live.hub import household_channel, member_channel
from app.redis import get_redis

router = APIRouter(tags=["live"])

UNAUTHORIZED = 4401
DISCONNECTED = 4403


async def _forward(ws: WebSocket, pubsub) -> None:
    async for item in pubsub.listen():
        if item["type"] != "message":
            continue
        data = item["data"]
        try:
            kind = json.loads(data).get("kind")
        except (ValueError, AttributeError):
            continue
        if kind == "disconnect":
            await ws.close(code=DISCONNECTED)
            return
        await ws.send_text(data)


@router.websocket("/ws")
async def live(
    ws: WebSocket,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    token: str = "",
) -> None:
    member = await member_from_token(session, token)
    await session.commit()  # end the read transaction: don't pin a connection for the socket's life
    if member is None:
        await ws.accept()
        await ws.close(code=UNAUTHORIZED)
        return
    pubsub = redis.pubsub()
    try:
        # subscribe before accepting so nothing published after the handshake is missed
        await pubsub.subscribe(member_channel(member.id), household_channel(member.household_id))
        await ws.accept()

        async def _hangup() -> None:
            while True:  # clients send nothing meaningful; this detects hang-up
                await ws.receive_text()

        tasks = [asyncio.create_task(_forward(ws, pubsub)), asyncio.create_task(_hangup())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()
