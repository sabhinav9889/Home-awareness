"""The live socket authenticates with `/ws?token=...`, and uvicorn logs the full path with its
query string ('"WebSocket /ws?token=..." [accepted]') on `uvicorn.error` at INFO. Served by a
real uvicorn in this event loop so the test session and fakeredis overrides still apply."""

import asyncio
import logging
import socket

import httpx
import pytest
import uvicorn
from httpx_ws import aconnect_ws

import app.main as app_main
from app.live.hub import publish_to_member
from app.logging import configure_logging, redact
from tests.test_agent_tokens import _mint
from tests.test_live import _recv, _tok

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")


def test_redact_masks_query_tokens_and_keeps_the_rest():
    line = '127.0.0.1:5 - "WebSocket /ws?token=ha_agent_AbC-1_x.y&x=1" [accepted]'
    assert redact(line) == '127.0.0.1:5 - "WebSocket /ws?token=***&x=1" [accepted]'
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln-_x"
    assert redact(f"GET /ws?token={jwt} HTTP/1.1") == "GET /ws?token=*** HTTP/1.1"
    assert redact("token='abc' other") == "token='abc' other"  # quote ends the value; nothing leaks
    assert redact("token=abc'def") == "token=***'def"
    assert redact("bot123:SECRET-x/sendMessage") == "bot***/sendMessage"  # still redacts bots


def test_configure_logging_quiets_uvicorn_error_info():
    configure_logging()
    assert logging.getLogger("uvicorn.error").getEffectiveLevel() >= logging.WARNING


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_ws_tokens_never_reach_uvicorn_logs(client, signed_up, redis, caplog):
    owner, oh = await signed_up()
    agent = (await _mint(client, oh))["token"]
    jwt = _tok(oh)
    configure_logging()
    # force INFO even past the WARNING belt: the redaction itself must hold
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    port = _free_port()
    config = uvicorn.Config(
        app_main.app, host="127.0.0.1", port=port, lifespan="off", log_config=None
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        async with httpx.AsyncClient() as c:
            for token in (agent, jwt):
                async with aconnect_ws(f"ws://127.0.0.1:{port}/ws?token={token}", c) as ws:
                    await asyncio.sleep(0.05)
                    await publish_to_member(redis, owner.id, {"kind": "alert", "alert_id": 1})
                    assert await _recv(ws) == {"kind": "alert", "alert_id": 1}
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "WebSocket /ws?token=***" in text, text  # positive control: uvicorn did log the path
    for secret in (agent, "ha_agent_", jwt):
        assert secret not in text
