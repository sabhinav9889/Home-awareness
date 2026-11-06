import logging
from email.message import EmailMessage
from typing import Protocol

import aiosmtplib

from app.config import get_settings

log = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    async def send(self, to: str, subject: str, body: str) -> None:
        log.info("email to=%s subject=%s\n%s", to, subject, body)


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
