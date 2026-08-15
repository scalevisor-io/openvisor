"""emailer TLS-mode selection: port 465 is implicit-TLS (SMTPS - the server
speaks TLS from the first byte), so send_email must open SMTP_SSL and never call
starttls(); any other port keeps the plaintext-then-STARTTLS upgrade. A 465
config on the STARTTLS path hangs on the greeting until the timeout."""
import smtplib

from app.core.config import settings
from app.services import emailer


class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.starttls_called = False
        self.login_args = None
        self.sent = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


class _FakeSMTPSSL(_FakeSMTP):
    instances: list = []


def _configure(monkeypatch, port):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)
    _FakeSMTP.instances, _FakeSMTPSSL.instances = [], []
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(settings, "smtp_port", port)
    monkeypatch.setattr(settings, "smtp_user", "user")
    monkeypatch.setattr(settings, "smtp_password", "pw")
    monkeypatch.setattr(settings, "smtp_from", "no-reply@example.com")


def test_port_465_uses_smtps_and_never_starttls(monkeypatch):
    _configure(monkeypatch, 465)
    assert emailer.send_email("to@example.com", "s", "b") is True
    assert _FakeSMTP.instances == []          # plaintext SMTP never opened
    (conn,) = _FakeSMTPSSL.instances
    assert conn.starttls_called is False
    assert conn.login_args == ("user", "pw")
    assert len(conn.sent) == 1


def test_other_ports_keep_starttls_upgrade(monkeypatch):
    _configure(monkeypatch, 587)
    assert emailer.send_email("to@example.com", "s", "b") is True
    assert _FakeSMTPSSL.instances == []
    (conn,) = _FakeSMTP.instances
    assert conn.starttls_called is True
    assert conn.login_args == ("user", "pw")
    assert len(conn.sent) == 1
