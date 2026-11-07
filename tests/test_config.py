import pytest
from pydantic import ValidationError

from app.config import Settings

BASE = {"database_url": "postgresql+asyncpg://x/y", "redis_url": "redis://x"}


@pytest.mark.parametrize(
    "secret", ["", "change-me", "changeme", "secret", "dev", "ci-secret", "x" * 31]
)
def test_weak_or_short_jwt_secret_rejected(secret):
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None, jwt_secret=secret, **BASE)


def test_strong_jwt_secret_accepted():
    assert Settings(_env_file=None, jwt_secret="s" * 32, **BASE).jwt_secret == "s" * 32


def _s(**kw) -> Settings:
    return Settings(_env_file=None, jwt_secret="s" * 32, **BASE, **kw)


@pytest.mark.parametrize("secret", ["has space", "dot.ted", "slash/x", "é", "x" * 257, "a=b"])
def test_telegram_webhook_secret_outside_telegrams_charset_fails_at_startup(secret):
    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET"):
        _s(telegram_webhook_secret=secret)


@pytest.mark.parametrize("secret", ["a", "Az09_-", "x" * 256])
def test_telegram_webhook_secret_in_charset_is_accepted(secret):
    assert _s(telegram_webhook_secret=secret).telegram_webhook_secret == secret


@pytest.mark.parametrize("secret", [None, ""])
def test_unset_or_empty_telegram_webhook_secret_means_disabled(secret):
    # Compose passes `${TELEGRAM_WEBHOOK_SECRET:-}`, i.e. "" when unset.
    assert _s(telegram_webhook_secret=secret).telegram_webhook_secret is None


def test_startup_errors_never_echo_the_secret():
    with pytest.raises(ValidationError) as e:
        _s(telegram_webhook_secret="leaky secret.value")
    assert "leaky secret.value" not in str(e.value)
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, jwt_secret="short-but-private", **BASE)
    assert "short-but-private" not in str(e.value)
