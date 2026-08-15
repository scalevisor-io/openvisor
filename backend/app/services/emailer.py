"""Outbound SMTP (transactional only, ultra-concise, PROMPT §22)."""
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> bool:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        # Port 465 is implicit-TLS (SMTPS): the server speaks TLS from the very
        # first byte, so a plaintext SMTP() + starttls() would hang on the
        # greeting. Every other port keeps the STARTTLS upgrade before login.
        smtps = int(settings.smtp_port) == 465
        conn = smtplib.SMTP_SSL if smtps else smtplib.SMTP
        with conn(settings.smtp_host, settings.smtp_port, timeout=15) as s:
            if settings.smtp_user:
                if not smtps:
                    s.starttls()
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return True
    except Exception as exc:  # never crash a flow because SMTP is down
        log.error("SMTP send to %s failed: %s", to, exc)
        return False
