"""Outbound SMTP (transactional only, PROMPT §22).

Every message goes out as multipart/alternative: the plain text the call site
wrote, and the branded HTML `email_render` builds from it. Call sites never see
the template - they write text, and `cta` only names the button label for the
URL on the body's last line.
"""
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from app.core.config import settings
from app.services import email_render

log = logging.getLogger(__name__)


def _from_header() -> str:
    """The sender with the brand as its display name, unless SMTP_FROM already
    carries one ("Acme <no-reply@acme.test>")."""
    name, address = parseaddr(settings.smtp_from)
    if not address:
        return settings.smtp_from
    return formataddr((name or settings.brand_name, address))


def send_email(to: str, subject: str, body: str, cta: str | None = None) -> bool:
    msg = EmailMessage()
    msg["From"] = _from_header()
    msg["To"] = to
    msg["Subject"] = subject
    text, html = email_render.render(subject, body, cta)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
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
