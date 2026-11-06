from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_JWT_SECRET_LEN = 32
WEAK_JWT_SECRETS = {"change-me", "changeme", "secret", "dev", "ci-secret"}


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
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None
    telegram_webhook_secret: str | None = None
    ntfy_base_url: str = "https://ntfy.sh"

    @field_validator("jwt_secret")
    @classmethod
    def _strong_secret(cls, v: str) -> str:
        # A guessable secret lets anyone mint a token for any household: refuse to start.
        if len(v) < MIN_JWT_SECRET_LEN or v.strip().lower() in WEAK_JWT_SECRETS:
            raise ValueError(
                f"JWT_SECRET must be a random string of at least {MIN_JWT_SECRET_LEN} "
                "characters; generate one with "
                'python -c "import secrets;print(secrets.token_urlsafe(48))"'
            )
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
