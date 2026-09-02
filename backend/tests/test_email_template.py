"""§22 branded email: every message leaves as multipart/alternative - the plain
text the call site wrote, byte for byte, plus the HTML template around it. The
text part is what the e2e harness scrapes verification links out of, so the two
invariants under test are "the body survives untouched" and "the HTML never
emits an unescaped fragment of it"."""
import re
import smtplib

import pytest

from app.core.config import settings
from app.services import email_render, emailer
from app.services.statuses import STATUSES, STATUS_LABELS, status_label


@pytest.fixture(autouse=True)
def _brand(monkeypatch):
    monkeypatch.setattr(settings, "brand_name", "acme.ai")
    monkeypatch.setattr(settings, "brand_color_primary", "#22d3ee")
    monkeypatch.setattr(settings, "landing_base_url", "https://acme.ai")
    monkeypatch.setattr(email_render, "_legal_cache",
                        (email_render.time.monotonic(),
                         {"name": "Acme SAS", "address": "1 Rue Test\n75001 Paris"}))


def test_html_carries_the_logo_lockup_and_legal_mentions():
    html = email_render.render_html("[acme.ai] Hello", "Body line.")
    # The mark is drawn, never fetched: a remote logo is the first thing a mail
    # client blocks, so the template must have no external reference at all.
    assert "<img" not in html and "src=" not in html
    assert ">acme<" in html and ">.ai</span>" in html
    assert "Acme SAS" in html and "1 Rue Test, 75001 Paris" in html
    assert "https://acme.ai/terms" in html and "https://acme.ai/privacy" in html


def test_text_part_keeps_the_body_verbatim():
    body = "Verify your email.\n\nhttps://app.acme.ai/verify?token=abc.DEF-123"
    text = email_render.render_text("[acme.ai] Verify your email", body)
    assert text.startswith(body)
    assert "https://app.acme.ai/verify?token=abc.DEF-123" in text
    assert "Acme SAS" in text and "1 Rue Test, 75001 Paris" in text


def test_trailing_url_becomes_the_button_and_takes_the_label():
    html = email_render.render_html(
        "[acme.ai] Status", "Project is now Payment due.\n\nhttps://app.acme.ai/projects/1",
        "Open the project")
    assert ">Open the project</a>" in html
    assert 'href="https://app.acme.ai/projects/1"' in html
    # ...and the URL is not ALSO left as a paragraph under the button.
    assert html.count("https://app.acme.ai/projects/1") == 1


def test_url_inside_a_sentence_stays_an_inline_link():
    html = email_render.render_html("[acme.ai] S", "See https://app.acme.ai/x now.")
    assert 'href="https://app.acme.ai/x"' in html
    assert "</a> now." in html          # trailing period is sentence, not URL
    assert "<td bgcolor=" in html       # header mark only, no button table
    assert ">Open in acme.ai</a>" not in html


def test_body_is_html_escaped():
    html = email_render.render_html("[acme.ai] <b>subj</b>", "5 > 3 & <script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>subj</b>" not in html


def test_dash_bullets_become_a_list():
    html = email_render.render_html("[acme.ai] S", "Findings:\n- first\n- second")
    assert html.count("<li") == 2 and ">first</li>" in html


def test_legal_mentions_degrade_when_unavailable(monkeypatch):
    monkeypatch.setattr(email_render, "_legal_cache", None)
    monkeypatch.setattr(email_render, "_legal_identity", lambda: {"name": "", "address": ""})
    html = email_render.render_html("[acme.ai] S", "Body.")
    assert "acme.ai." in html and "Registered address" not in html


def test_button_text_stays_readable_on_a_dark_brand_colour(monkeypatch):
    monkeypatch.setattr(settings, "brand_color_primary", "#101010")
    html = email_render.render_html("[acme.ai] S", "Body.\n\nhttps://app.acme.ai/x")
    assert "#ffffff" in html.split("<td bgcolor=\"#101010\"")[1][:400]


def test_every_status_has_a_reader_facing_label():
    assert set(STATUS_LABELS) == STATUSES
    assert status_label("payment_due") == "Payment due"
    assert status_label("nonsense") == "nonsense"


# ---- the wire: what actually reaches the SMTP server ----

class _FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        type(self).sent.append(msg)


def test_send_email_is_multipart_with_a_branded_sender(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_from", "no-reply@acme.ai")
    _FakeSMTP.sent = []

    body = "Project moved.\n\nhttps://app.acme.ai/projects/1"
    assert emailer.send_email("to@example.com", "[acme.ai] S", body, "Open the project") is True

    (msg,) = _FakeSMTP.sent
    # formataddr quotes a display name holding a dot; clients show "acme.ai".
    assert msg["From"] == '"acme.ai" <no-reply@acme.ai>'
    assert msg.get_content_type() == "multipart/alternative"
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert text.startswith(body)
    assert ">Open the project</a>" in html
    # A link scraper (the e2e harness) still finds the URL in the text part.
    assert re.search(r"https://app\.acme\.ai/projects/1", text)


def test_send_email_keeps_a_display_name_smtp_from_already_has(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_from", "Acme Support <help@acme.ai>")
    _FakeSMTP.sent = []
    assert emailer.send_email("to@example.com", "[acme.ai] S", "Body.") is True
    assert _FakeSMTP.sent[-1]["From"] == "Acme Support <help@acme.ai>"


def test_a_malformed_brand_colour_cannot_escape_an_attribute(monkeypatch):
    monkeypatch.setattr(settings, "brand_color_primary", '"><script>x</script>')
    html = email_render.render_html("[acme.ai] S", "Body.\n\nhttps://app.acme.ai/x")
    assert "<script>" not in html
    assert 'bgcolor="#22d3ee"' in html
