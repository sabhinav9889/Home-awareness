import logging


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every request URL at INFO, and Telegram's URL embeds the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
