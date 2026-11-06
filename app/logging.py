import json
import logging
import re
import time
import uuid

# Telegram's URL embeds the bot token: https://api.telegram.org/bot<id>:<secret>/...
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_installed = False


def redact(text: str) -> str:
    return _TOKEN_RE.sub("bot***", text)


def protect_logs() -> None:
    """Keep the Telegram bot token out of every log, in every process (API and Celery).

    Idempotent. A record factory (not a handler filter) so it applies whatever handlers a
    framework installs later, and httpx/httpcore are quieted since they log request URLs.
    """
    global _installed
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if _installed:
        return
    _installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return record

    logging.setLogRecordFactory(factory)


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    protect_logs()


request_log = logging.getLogger("app.request")


class RequestLogMiddleware:
    """One JSON line per request. Logs the matched route template, never the raw path or
    query string (`/ws?token=<JWT>`, `/telegram/webhook/<secret>`), and no headers or bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            elif message["type"] == "websocket.accept":
                status = 101
            elif message["type"] == "websocket.close" and status == 500:
                status = 403  # closed before accept = handshake rejected
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")  # set by the router once a route matched
            request_log.info(
                json.dumps(
                    {
                        "method": "WS" if scope["type"] == "websocket" else scope["method"],
                        "path": getattr(route, "path", None) or "unmatched",
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        "request_id": request_id,
                    }
                )
            )
