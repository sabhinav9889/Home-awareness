import logging
import re

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
