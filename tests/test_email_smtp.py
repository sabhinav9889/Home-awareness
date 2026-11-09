"""SmtpEmailSender must not attempt a login (or STARTTLS) when credentials are empty."""

from unittest.mock import AsyncMock

import pytest

from app import email as email_module
from app.email import SmtpEmailSender


@pytest.fixture
def send(monkeypatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(email_module.aiosmtplib, "send", mock)
    return mock


@pytest.mark.parametrize("user,password", [("", ""), (None, None)])
async def test_empty_credentials_mean_no_login(send, user, password):
    await SmtpEmailSender("smtp.example.org", 25, user, password, "from@example.org").send(
        "to@example.org", "Subject", "Body"
    )
    kwargs = send.await_args.kwargs
    assert kwargs["username"] is None
    assert kwargs["password"] is None
    assert kwargs["start_tls"] is None
    assert kwargs["hostname"] == "smtp.example.org"


async def test_credentials_are_passed_and_starttls_enabled(send):
    await SmtpEmailSender("smtp.example.org", 587, "mailer", "pw", "from@example.org").send(
        "to@example.org", "Subject", "Body"
    )
    kwargs = send.await_args.kwargs
    assert kwargs["username"] == "mailer" and kwargs["password"] == "pw"
    assert kwargs["start_tls"] is True
