import sys
from email.message import EmailMessage
from typing import Protocol

import aiosmtplib

from app.config import get_settings


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    """Dev backend: the email *is* the output. Written straight to stderr, not through
    logging, because the log redaction masks `token=` values and these links must stay usable
    (`docker compose logs api | grep token=`). Never use it where logs are shared."""

    async def send(self, to: str, subject: str, body: str) -> None:
        print(f"email to={to} subject={subject}\n{body}", file=sys.stderr, flush=True)


class SmtpEmailSender:
    def __init__(
        self,
        host: str,
        port: int | None,
        user: str | None,
        password: str | None,
        sender: str,
    ) -> None:
        self.host, self.port, self.user, self.password, self.sender = (
            host,
            port,
            user,
            password,
            sender,
        )

    async def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        await aiosmtplib.send(
            msg,
            hostname=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            start_tls=True if self.user else None,
        )


def get_email_sender() -> EmailSender:
    s = get_settings()
    if s.email_backend == "smtp":
        if not s.smtp_host or not s.smtp_from:
            raise RuntimeError("EMAIL_BACKEND=smtp requires SMTP_HOST and SMTP_FROM")
        return SmtpEmailSender(s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password, s.smtp_from)
    return ConsoleEmailSender()
