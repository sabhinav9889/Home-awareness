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
