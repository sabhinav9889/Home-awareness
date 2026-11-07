"""The console email backend is the dev delivery channel (README: `docker compose logs api |
grep token=`), so the `token=` log redaction must not swallow its links."""

from app.email import ConsoleEmailSender
from app.logging import configure_logging


async def test_console_email_still_shows_the_link_after_log_redaction(capsys):
    configure_logging()
    await ConsoleEmailSender().send("o@example.com", "Confirm", "Go: http://x/confirm?token=cf_abc")
    out = capsys.readouterr()
    assert "token=cf_abc" in out.out + out.err
