"""§Tools `email` kind: notification email the dev agent can send, to approved
addresses only.

The row's SMTP settings live in `Tool.params["smtp"]` (the password
envelope-encrypted as `password_enc`, never returned) and the admin's recipient
allowlist in `Tool.params["allowed_recipients"]`. `api_key_enc` stays empty on
purpose: every generic §Tools path forwards that column to the MCP endpoint as a
Bearer, and an SMTP password must not ride anywhere.

The trust boundary is the point of the design. Whatever reaches the sandbox is
readable by the agent and by any code it runs, so a build never receives SMTP
credentials - if it did, the recipient rule would be a suggestion. It receives a
short-lived token bound to its project (`mint_token`). The sidecar
(sidecars/email.py) relays tool calls with that token to the api's internal
route (api/email_tool.py), which resolves the project, re-reads the row, checks
every recipient against `allowed_recipients()` and only then talks to SMTP.

Who may receive mail: the project's customer - every verified, unblocked
account of the organization that owns the project, which is exactly who holds
owner access to it (`deps.project_access_role`), the oldest (the address every
other project notification goes to) first - plus the addresses the admin listed
on /admin/tools. Nothing the agent sends can widen that set. An unverified
account is left out on purpose: nobody has proven they own that inbox.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy import select

from app.core.config import settings
from app.core.encryption import decrypt, encrypt
from app.models import Project, ProjectToolConfig, Tool, User

SLUG = "email"
KIND = "email"
NAME = "Email notifications"

SECURITY_MODES = ("starttls", "ssl", "none")
DEFAULT_PORT = {"starttls": 587, "ssl": 465, "none": 25}

MAX_RECIPIENTS = 10
MAX_SUBJECT = 200
MAX_BODY = 20_000
MAX_ALLOWLIST = 50
# Per project, per rolling hour. A notification tool that needs more than this
# is a mailing tool, which this is not.
SENDS_PER_HOUR = 20
# On top of the run's own timeout: plan stage, queueing, publish.
TOKEN_MARGIN_S = 1800

_SMTP_TIMEOUT = 20.0
_TOKEN_TAG = "et1"
# Deliberately narrow: no spaces, commas, angle brackets, quotes or line breaks,
# so one "address" can never smuggle a second recipient or a header.
_ADDRESS_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")


class EmailToolError(Exception):
    """A send that did not happen. The message reaches the agent: never put the
    password in it."""


def endpoint(base_url: str) -> str:
    """The email route on the sidecars-mcp image."""
    return f"{base_url.rstrip('/')}/email/mcp"


# ── addresses ────────────────────────────────────────────────────────────

def normalize_address(raw: str) -> str | None:
    """Lower-cased address, or None when it is not one plain address."""
    addr = (raw or "").strip().lower()
    return addr if len(addr) <= 254 and _ADDRESS_RE.fullmatch(addr) else None


def _dedupe(addrs) -> list[str]:
    seen: dict[str, None] = {}
    for a in addrs:
        if a:
            seen.setdefault(a, None)
    return list(seen)


# ── row configuration ────────────────────────────────────────────────────

def _smtp(t: Tool) -> dict:
    return dict((t.params or {}).get("smtp") or {})


def admin_recipients(t: Tool) -> list[str]:
    return list((t.params or {}).get("allowed_recipients") or [])


def is_configured(t: Tool) -> bool:
    smtp = _smtp(t)
    return bool(smtp.get("host") and smtp.get("from_address"))


def public_config(t: Tool) -> dict:
    """What the admin page shows. The password is reduced to a flag."""
    smtp = _smtp(t)
    security = smtp.get("security") or "starttls"
    return {"smtp_host": smtp.get("host") or "",
            "smtp_port": smtp.get("port") or DEFAULT_PORT[security],
            "smtp_security": security,
            "smtp_username": smtp.get("username") or "",
            "has_password": bool(smtp.get("password_enc")),
            "from_address": smtp.get("from_address") or "",
            "from_name": smtp.get("from_name") or "",
            "allowed_recipients": admin_recipients(t),
            "configured": is_configured(t)}


def apply_config(t: Tool, body) -> bool:
    """Apply an `EmailToolConfigIn` patch (None = leave as is, "" = clear).
    Returns whether anything SMTP-facing changed, which is what obliges the
    caller to re-probe an enabled row. Raises ValueError with a user-facing
    message on bad input."""
    smtp = _smtp(t)
    before = dict(smtp)
    fields = body.model_fields_set
    if "smtp_host" in fields and body.smtp_host is not None:
        host = body.smtp_host.strip()
        if host and not _HOST_RE.fullmatch(host):
            raise ValueError("The SMTP host must be a hostname or an IP address")
        smtp["host"] = host
    if "smtp_security" in fields and body.smtp_security is not None:
        smtp["security"] = body.smtp_security
    if "smtp_port" in fields and body.smtp_port is not None:
        smtp["port"] = body.smtp_port
    if "smtp_username" in fields and body.smtp_username is not None:
        smtp["username"] = body.smtp_username.strip()
    if "smtp_password" in fields and body.smtp_password is not None:
        smtp["password_enc"] = encrypt(body.smtp_password) if body.smtp_password else None
    if "from_address" in fields and body.from_address is not None:
        raw = body.from_address.strip()
        addr = normalize_address(raw) if raw else ""
        if addr is None:
            raise ValueError(f"Not an email address: {raw[:80]}")
        smtp["from_address"] = addr
    if "from_name" in fields and body.from_name is not None:
        smtp["from_name"] = " ".join(body.from_name.split())
    params = dict(t.params or {})
    if "allowed_recipients" in fields and body.allowed_recipients is not None:
        out = []
        for raw in body.allowed_recipients:
            if not (raw or "").strip():
                continue
            addr = normalize_address(raw)
            if addr is None:
                raise ValueError(f"Not an email address: {raw.strip()[:80]}")
            out.append(addr)
        out = _dedupe(out)
        if len(out) > MAX_ALLOWLIST:
            raise ValueError(f"At most {MAX_ALLOWLIST} approved addresses")
        params["allowed_recipients"] = out
    smtp.setdefault("security", "starttls")
    if not smtp.get("port"):
        smtp["port"] = DEFAULT_PORT[smtp["security"]]
    params["smtp"] = smtp
    t.params = params
    return smtp != before


# ── SMTP ─────────────────────────────────────────────────────────────────

def _open(t: Tool) -> smtplib.SMTP:
    smtp = _smtp(t)
    host, port = smtp.get("host"), int(smtp.get("port") or 587)
    security = smtp.get("security") or "starttls"
    ctx = ssl.create_default_context()
    if security == "ssl":
        conn = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT, context=ctx)
    else:
        conn = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
    try:
        conn.ehlo()
        if security == "starttls":
            conn.starttls(context=ctx)
            conn.ehlo()
        if smtp.get("username"):
            password = decrypt(smtp["password_enc"]) if smtp.get("password_enc") else ""
            conn.login(smtp["username"], password)
    except BaseException:
        conn.close()
        raise
    return conn


def _describe(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the SMTP server rejected the username or password"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "the SMTP server refused the recipient(s)"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "the SMTP server refused the From address"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "the SMTP server does not support that (try another security mode)"
    if isinstance(exc, smtplib.SMTPResponseException):
        msg = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"the SMTP server answered {exc.smtp_code}: {' '.join(msg.split())[:160]}"
    if isinstance(exc, ssl.SSLError):
        return "TLS failed (check the port and the security mode)"
    if isinstance(exc, (TimeoutError, OSError)):
        return f"could not reach the SMTP server ({type(exc).__name__})"
    return f"SMTP error ({type(exc).__name__})"


def probe(t: Tool) -> tuple[bool, str]:
    """(ok, detail): connect, negotiate TLS, log in - and send nothing. Sync,
    called via run_in_threadpool."""
    if not is_configured(t):
        return False, "set the SMTP host and the From address first"
    try:
        conn = _open(t)
    except Exception as exc:  # noqa: BLE001 - every failure is the admin's to read
        return False, _describe(exc)
    try:
        conn.quit()
    except Exception:  # noqa: BLE001 - the login already succeeded
        pass
    return True, ""


def send(t: Tool, to: list[str], subject: str, body: str) -> None:
    """Send one message to `to` (already checked against the allowlist). Sync.
    Raises EmailToolError."""
    from app.services import email_render

    smtp = _smtp(t)
    subject = " ".join((subject or "").split())[:MAX_SUBJECT]
    msg = EmailMessage()
    msg["From"] = formataddr((smtp.get("from_name") or settings.brand_name,
                              smtp["from_address"]))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    text, html = email_render.render(subject, body)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        conn = _open(t)
        try:
            conn.send_message(msg)
        finally:
            try:
                conn.quit()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        raise EmailToolError(_describe(exc)) from None


# ── who may receive ──────────────────────────────────────────────────────

def _customer_query(project: Project):
    """The accounts with owner access to the project, oldest first."""
    return (select(User.email)
            .where(User.org_id == project.org_id, User.email_verified.is_(True),
                   User.blocked.is_(False))
            .order_by(User.created_at))


async def customer_addresses(db, project: Project) -> list[str]:
    found = (await db.execute(_customer_query(project))).scalars().all()
    return _dedupe(normalize_address(a or "") for a in found)


async def recipients(db, project: Project, t: Tool) -> list[dict]:
    """Every address this project's builds may write to, labelled."""
    customers = await customer_addresses(db, project)
    out = [{"address": a, "label": "project customer"} for a in customers]
    out += [{"address": a, "label": "approved by the administrator"}
            for a in admin_recipients(t) if a not in customers]
    return out


async def effective_enabled(db, t: Tool, project_id: str) -> bool:
    ov = (await db.execute(select(ProjectToolConfig).where(
        ProjectToolConfig.project_id == project_id,
        ProjectToolConfig.tool_id == t.id))).scalar_one_or_none()
    return ov.enabled if (ov is not None and ov.enabled is not None) else t.enabled


# ── the run token ────────────────────────────────────────────────────────

def _key() -> bytes:
    # Derived, so the token can never double as anything else signed with the
    # app secret.
    return hmac.new(settings.secret_key.encode(), b"email-tool-token/v1",
                    hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_token(project_id: str, ttl_s: int) -> str:
    """The credential a build's mcp.json carries for this tool: the project id
    and an expiry, signed. It authorises nothing but sending as that project to
    that project's approved addresses."""
    payload = f"{project_id}:{int(time.time()) + int(ttl_s)}".encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_TOKEN_TAG}.{_b64(payload)}.{_b64(sig)}"


def read_token(token: str | None) -> str | None:
    """The project id a valid, unexpired token was minted for, else None."""
    try:
        tag, payload_b64, sig_b64 = (token or "").split(".")
        if tag != _TOKEN_TAG:
            return None
        payload = _unb64(payload_b64)
        if not hmac.compare_digest(
                hmac.new(_key(), payload, hashlib.sha256).digest(), _unb64(sig_b64)):
            return None
        project_id, exp = payload.decode().rsplit(":", 1)
        if int(exp) < time.time():
            return None
        return project_id or None
    except (ValueError, UnicodeDecodeError):
        return None
