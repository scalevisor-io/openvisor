"""Branded HTML for every transactional email (PROMPT §22).

Call sites keep writing plain text: `emailer.send_email` runs that text through
`render()` and sends the multipart/alternative pair, so the template can change
without touching the twenty places that send mail.

Two constraints shape the markup. Mail clients block remote images by default,
so a logo fetched over http would arrive as an empty box - the mark is drawn
with a table cell and the wordmark is text, and the whole thing survives image
blocking, Gmail, Outlook and dark mode with nothing to load. And the brand is
white-label (BRAND_NAME, BRAND_COLOR_*), so every colour that carries text is
paired against the background it lands on rather than hardcoded readable.

The footer's legal mentions come from the admin's §legal identity (AppSetting,
/admin/settings) with a short TTL cache: mail is low-volume, and an unreachable
database degrades to the brand name instead of failing the send.
"""
import html
import logging
import re
import time
from datetime import datetime, timezone

from app.core.config import settings

log = logging.getLogger(__name__)

# Surface palette, matching the SPA/landing tokens (global.css --gray-*).
BG = "#090b11"
CARD = "#0f131c"
BORDER = "#283044"
MARK_BG = "#141925"
TEXT = "#e3e6ee"
TEXT_STRONG = "#ffffff"
TEXT_MUTED = "#8490b5"
FONT = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, "
        "Arial, sans-serif")

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
# Trailing punctuation that ends the sentence rather than the URL.
_URL_TRIM = ".,;:!?)]}'\""

_LEGAL_TTL_SECONDS = 300
_legal_cache: tuple[float, dict[str, str]] | None = None


# ---------------------------------------------------------------- colour

def _luminance(hex_color: str) -> float:
    """Relative luminance (0 = black, 1 = white) of a #rgb / #rrggbb string."""
    value = hex_color.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return 0.5
    try:
        r, g, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 0.5
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _on_accent(accent: str) -> str:
    """Text colour for a button filled with `accent`."""
    return "#06121a" if _luminance(accent) > 0.5 else TEXT_STRONG


def _accent_on_dark(accent: str) -> str:
    """The accent itself, or a light substitute when it would sink into the
    dark surface (a brand may pick a near-black primary)."""
    return accent if _luminance(accent) > 0.22 else TEXT


_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _accent() -> str:
    """BRAND_COLOR_PRIMARY, but only ever a hex literal: the value lands inside
    HTML attributes, and an env var is not a reason to trust a string."""
    value = (settings.brand_color_primary or "").strip()
    return value if _HEX_RE.fullmatch(value) else "#22d3ee"


# ---------------------------------------------------------------- legal mentions

def _legal_identity() -> dict[str, str]:
    """The operating company's name and registered address, cached briefly.
    Empty strings when the admin never set them, or when the DB is unreachable."""
    global _legal_cache
    now = time.monotonic()
    if _legal_cache is not None and now - _legal_cache[0] < _LEGAL_TTL_SECONDS:
        return _legal_cache[1]
    out = {"name": "", "address": ""}
    try:
        from app.core.db import SyncSession
        from app.services.app_settings import (LEGAL_ADDRESS, LEGAL_NAME,
                                               get_setting_sync)
        with SyncSession() as db:
            out = {
                "name": str(get_setting_sync(db, LEGAL_NAME, "") or "").strip(),
                "address": str(get_setting_sync(db, LEGAL_ADDRESS, "") or "").strip(),
            }
    except Exception as exc:  # noqa: BLE001 - mentions degrade, mail still goes out
        log.warning("legal identity unavailable for the email footer: %s", exc)
    _legal_cache = (now, out)
    return out


def reset_legal_cache() -> None:
    """Drop the cached §legal identity (admin just edited it, or a test)."""
    global _legal_cache
    _legal_cache = None


# ---------------------------------------------------------------- body -> blocks

def _bare_url(line: str) -> str | None:
    """The URL when the line is nothing but a URL, else None."""
    stripped = line.strip()
    return stripped if _URL_RE.fullmatch(stripped) else None


def _autolink(text: str) -> str:
    """HTML-escape, then turn bare URLs into anchors."""
    accent = _accent_on_dark(_accent())
    out, last = [], 0
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TRIM)
        out.append(html.escape(text[last:match.start()]))
        out.append(f'<a href="{html.escape(url, quote=True)}" '
                   f'style="color:{accent};text-decoration:underline;">'
                   f'{html.escape(url)}</a>')
        last = match.start() + len(url)
    out.append(html.escape(text[last:]))
    return "".join(out)


def _blocks(body: str) -> list[tuple[str, object]]:
    """Group the plain-text body into ("p" | "ul" | "url", content) blocks.
    Blank lines separate paragraphs, "- " lines become a list, and a line that
    is nothing but a URL stands alone (a candidate for the button)."""
    out: list[tuple[str, object]] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            out.append(("p", "\n".join(para)))
            para.clear()
        if items:
            out.append(("ul", list(items)))
            items.clear()

    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.lstrip().startswith("- "):
            if para:
                flush()
            items.append(line.lstrip()[2:])
            continue
        if items:
            flush()
        bare = _bare_url(line)
        if bare:
            flush()
            out.append(("url", bare))
            continue
        para.append(line)
    flush()
    return out


# ---------------------------------------------------------------- template

def _title(subject: str) -> str:
    """The subject without the "[brand] " prefix brand.subject() stamps on."""
    prefix = f"[{settings.brand_name}] "
    return subject[len(prefix):] if subject.startswith(prefix) else subject


def _wordmark(accent: str) -> str:
    """The brand name, two-tone on its last dot like the landing wordmark."""
    name = settings.brand_name
    dot = name.rfind(".")
    head, tail = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    out = html.escape(head)
    if tail:
        out += f'<span style="color:{accent};">{html.escape(tail)}</span>'
    return out


def _preheader(body: str) -> str:
    """Inbox preview text: the first real sentence, never a raw URL."""
    for line in body.splitlines():
        text = line.strip()
        if text and not _URL_RE.fullmatch(text):
            return html.escape(text[:140])
    return html.escape(settings.brand_name)


def _one_line(address: str) -> str:
    """A multi-line registered address on one comma-separated line."""
    return ", ".join(part.strip() for part in address.splitlines() if part.strip())


def _footer_lines() -> list[str]:
    """The legal mentions, as escaped HTML fragments."""
    legal = _legal_identity()
    year = datetime.now(timezone.utc).year
    brand_name = html.escape(settings.brand_name)
    entity = html.escape(legal["name"]) if legal["name"] else ""
    owner = f"{brand_name} - a {entity} service." if entity else f"{brand_name}."
    lines = [f"&copy; {year} {owner}"]
    if legal["address"]:
        address = html.escape(_one_line(legal["address"]))
        lines.append(f"Registered address: {address}")
    return lines


def _links_html(accent: str) -> str:
    base = (settings.landing_base_url or "").rstrip("/")
    if not base:
        return ""
    style = f"color:{accent};text-decoration:underline;"
    return (f'<a href="{html.escape(base, quote=True)}/terms" style="{style}">Terms of service</a>'
            f'<span style="color:{TEXT_MUTED};"> &middot; </span>'
            f'<a href="{html.escape(base, quote=True)}/privacy" style="{style}">Privacy policy</a>')


def _button(url: str, label: str, accent: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:24px 0 4px;"><tr>'
        f'<td bgcolor="{accent}" style="background:{accent};border-radius:10px;">'
        f'<a href="{html.escape(url, quote=True)}" '
        f'style="display:inline-block;padding:13px 24px;font-family:{FONT};'
        f'font-size:15px;font-weight:600;line-height:1;color:{_on_accent(accent)};'
        'text-decoration:none;border-radius:10px;">'
        f'{html.escape(label)}</a></td></tr></table>'
    )


def render_html(subject: str, body: str, cta: str | None = None) -> str:
    accent = _accent()
    link = _accent_on_dark(accent)
    blocks = _blocks(body)

    # A URL on the last line is the message's call to action ("here is the page
    # this is about"); anywhere else it stays an inline link in its paragraph.
    action: str | None = None
    if blocks and blocks[-1][0] == "url":
        action = str(blocks.pop()[1])

    parts: list[str] = []
    for kind, content in blocks:
        if kind == "ul":
            items = "".join(
                f'<li style="margin:0 0 6px;">{_autolink(str(i))}</li>'
                for i in content)  # type: ignore[union-attr]
            parts.append(
                f'<ul style="margin:0 0 16px;padding-left:20px;color:{TEXT};'
                f'font-family:{FONT};font-size:15px;line-height:23px;">{items}</ul>')
        else:
            text = _autolink(str(content)).replace("\n", "<br>")
            parts.append(
                f'<p style="margin:0 0 16px;color:{TEXT};font-family:{FONT};'
                f'font-size:15px;line-height:23px;word-break:break-word;">{text}</p>')
    if action:
        parts.append(_button(action, cta or f"Open in {settings.brand_name}", accent))

    footer = "<br>".join(_footer_lines())
    links = _links_html(link)
    initial = html.escape((settings.brand_name.strip() or "o")[0])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<meta name="supported-color-schemes" content="dark light">
<title>{html.escape(_title(subject))}</title>
</head>
<body style="margin:0;padding:0;width:100%;background-color:{BG};">
<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;color:transparent;font-size:1px;line-height:1px;">{_preheader(body)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{BG}" style="background-color:{BG};">
<tr><td align="center" style="padding:32px 16px 40px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="width:100%;max-width:600px;">

<tr><td style="padding:0 4px 18px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="38" height="38" align="center" valign="middle" bgcolor="{MARK_BG}" style="width:38px;height:38px;background-color:{MARK_BG};border-radius:11px;font-family:{FONT};font-size:20px;font-weight:700;line-height:38px;color:{_accent_on_dark(accent)};">{initial}</td>
<td style="padding-left:11px;font-family:{FONT};font-size:18px;font-weight:700;color:{TEXT_STRONG};">{_wordmark(_accent_on_dark(accent))}</td>
</tr></table>
</td></tr>

<tr><td bgcolor="{CARD}" style="background-color:{CARD};border:1px solid {BORDER};border-radius:14px;padding:28px 28px 26px;">
<h1 style="margin:0 0 18px;font-family:{FONT};font-size:19px;font-weight:600;line-height:26px;color:{TEXT_STRONG};">{html.escape(_title(subject))}</h1>
{"".join(parts)}
</td></tr>

<tr><td style="padding:22px 6px 0;font-family:{FONT};font-size:12px;line-height:19px;color:{TEXT_MUTED};">
<p style="margin:0 0 8px;color:{TEXT_MUTED};font-family:{FONT};font-size:12px;line-height:19px;">Automated message from {html.escape(settings.brand_name)} - this mailbox is not monitored, reply from the app.</p>
<p style="margin:0 0 8px;color:{TEXT_MUTED};font-family:{FONT};font-size:12px;line-height:19px;">{footer}</p>
{f'<p style="margin:0;font-family:{FONT};font-size:12px;line-height:19px;">{links}</p>' if links else ""}
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""


def render_text(subject: str, body: str) -> str:
    """The plain-text alternative: the body exactly as the call site wrote it,
    plus the same legal mentions. Never reflow it - the verification links the
    e2e harness scrapes have to survive byte for byte."""
    legal = _legal_identity()
    year = datetime.now(timezone.utc).year
    owner = (f"{settings.brand_name} - a {legal['name']} service."
             if legal["name"] else f"{settings.brand_name}.")
    lines = [body.rstrip(), "", "--",
             f"Automated message from {settings.brand_name} - this mailbox is not "
             "monitored, reply from the app.",
             f"(c) {year} {owner}"]
    if legal["address"]:
        lines.append(f"Registered address: {_one_line(legal['address'])}")
    base = (settings.landing_base_url or "").rstrip("/")
    if base:
        lines.append(f"Terms: {base}/terms - Privacy: {base}/privacy")
    return "\n".join(lines) + "\n"


def render(subject: str, body: str, cta: str | None = None) -> tuple[str, str]:
    """`(text, html)` for one message."""
    return render_text(subject, body), render_html(subject, body, cta)
