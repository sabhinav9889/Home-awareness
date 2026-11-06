import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import principal_from_token
from app.auth.models import AgentToken, Member
from app.db import get_session
from app.live.hub import household_channel, member_channel
from app.redis import get_redis

log = logging.getLogger(__name__)
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
    # A session JWT or an `ha-agent` token. A revoked token has no live push of its own:
    # its open socket lives on until the next reconnect, which then fails with 4401.
    principal = await principal_from_token(session, token)
    await session.commit()  # end the read transaction (and persist last_used_at)
    if principal is None:
        await ws.accept()
        await ws.close(code=UNAUTHORIZED)
        return
    member = principal.member
    pubsub = redis.pubsub()
    try:
        # subscribe before accepting so nothing published after the handshake is missed
        await pubsub.subscribe(member_channel(member.id), household_channel(member.household_id))
        # A removal between the auth check and the subscribe published its disconnect to nobody:
        # re-check with a fresh read now that we are listening.
        still = await session.scalar(
            select(Member.id).where(
                Member.id == member.id,
                Member.household_id == member.household_id,
                Member.email_confirmed,
            )
        )
        if still is not None and principal.kind == "jwt":
            # a password reset between the auth check and the subscribe bumped the version
            still = await session.scalar(
                select(Member.id).where(
                    Member.id == member.id, Member.token_version == principal.token_version
                )
            )
        if still is not None and principal.agent_token_id is not None:
            still = await session.scalar(
                select(AgentToken.id).where(
                    AgentToken.id == principal.agent_token_id, AgentToken.revoked_at.is_(None)
                )
            )
        await session.commit()
        await ws.accept()
        if still is None:
            await ws.close(code=DISCONNECTED)
            return

        async def _hangup() -> None:
            while True:  # clients send nothing meaningful; this detects hang-up
                await ws.receive_text()

        tasks = [asyncio.create_task(_forward(ws, pubsub)), asyncio.create_task(_hangup())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            for res in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(res, Exception) and not isinstance(res, WebSocketDisconnect):
                    log.warning("live socket task failed", exc_info=res)
    finally:
        try:
            await pubsub.unsubscribe()
        finally:
            await pubsub.aclose()
