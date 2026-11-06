import logging

import httpx
import respx

from app.alerts import telegram

TOKEN = "123:SECRET-TOKEN_x"


async def _send_and_log(caplog):
    caplog.set_level(logging.INFO)
    respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as http:
        await telegram.send_message(http, TOKEN, "42", "hi")
    logging.getLogger("app.test").info("url https://api.telegram.org/bot%s/x", TOKEN)


@respx.mock
async def test_celery_worker_logging_config_never_logs_bot_token(caplog):
    from app.alerts import celery_app

    celery_app.protect_logs()  # what the worker/beat run via Celery's logging signals
    await _send_and_log(caplog)
    assert TOKEN not in caplog.text
    assert "bot***" in caplog.text  # the filter redacts even non-httpx records


@respx.mock
async def test_app_configure_logging_never_logs_bot_token(caplog):
    from app.logging import configure_logging

    configure_logging()
    await _send_and_log(caplog)
    assert TOKEN not in caplog.text
