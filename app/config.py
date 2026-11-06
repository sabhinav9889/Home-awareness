from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jwt_secret: str
    jwt_ttl_minutes: int = 60 * 24
    public_base_url: str = "http://localhost:8000"
    job_runner: Literal["celery", "inprocess"] = "celery"
    email_backend: Literal["console", "smtp"] = "console"
    smtp_host: str | None = None
    smtp_port: str | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None
    telegram_webhook_secret: str | None = None
    ntfy_base_url: str = "https://ntfy.sh"


@lru_cache
def get_settings() -> Settings:
    return Settings()
