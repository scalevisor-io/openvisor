"""§Tools `email`: the platform side of the notification-email tool.

The property everything here protects: a build can only ever make mail arrive
at its project's customer or at an address the admin approved. That holds only
because the build never holds the SMTP credentials - it holds a token bound to
its project - and because the api re-reads every rule (row enabled for the
project, recipient on the list, quota left) on each send. So the tests pin:

  - the seeded row, and that its password is write-only and never stored where
    a generic §Tools path would forward it (`api_key_enc`);
  - the run token: bound to one project, expiring, unforgeable;
  - what reaches a build's mcp.json: the token, never the password;
  - the enable gate: the SMTP login is proven server-side, and re-proven when an
    enabled row's SMTP settings change;
  - the internal send route: recipients, per-project disable, quota, failures,
    and the chat record the customer sees.
"""
import json
import smtplib
import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import decrypt, encrypt
from app.models import Message, Organization, Project, ProjectToolConfig, Tool, User
from app.seed import seed_tools
from app.services import email_tool, events, mcp_names
from app.workers import tasks

SMTP_OK = {"host": "smtp.example.com", "port": 587, "security": "starttls",
           "username": "mailer", "password_enc": None,
           "from_address": "notify@example.com", "from_name": "Builds"}


def _row(db) -> Tool:
    return db.execute(select(Tool).where(Tool.slug == email_tool.SLUG)).scalar_one()


def _reset():
    with SyncSession() as db:
        db.execute(delete(ProjectToolConfig))
        t = _row(db)
        t.enabled = False
        t.api_key_enc = None
        t.params = {"smtp": {"security": "starttls", "port": 587}, "allowed_recipients": []}
        db.commit()


def _configure(enabled=True, allowed=(), password="s3cret-pw"):
    with SyncSession() as db:
        t = _row(db)
        t.params = {"smtp": {**SMTP_OK, "password_enc": encrypt(password)},
                    "allowed_recipients": list(allowed)}
        t.enabled = enabled
        db.commit()
        return t.id


@pytest.fixture(autouse=True)
def seeded():
    seed_tools()
    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _clean_scan(monkeypatch):
    from app.services import mcp_scan
    monkeypatch.setattr(mcp_scan, "audit_server", lambda url, api_key=None: ([], None))
    monkeypatch.setattr(mcp_scan, "fingerprint_server", lambda url, api_key=None: "fp")
    monkeypatch.setattr(tasks, "_vet_mcp_server", lambda *a, **kw: True)


@pytest.fixture
def project():
    """An org with an owner, a second verified member, and two accounts that
    must never be written to: one unverified, one blocked."""
    with SyncSession() as db:
        org = Organization(name="Email Org")
        db.add(org)
        db.flush()
        tag = uuid.uuid4().hex[:8]
        owner = User(org_id=org.id, email=f"Owner-{tag}@Example.org", password_hash="x",
                     email_verified=True)
        db.add(owner)
        db.flush()
        member = User(org_id=org.id, email=f"member-{tag}@example.org", password_hash="x",
                      email_verified=True)
        db.add(member)
        db.flush()
        db.add(User(org_id=org.id, email=f"unverified-{tag}@example.org", password_hash="x"))
        db.add(User(org_id=org.id, email=f"blocked-{tag}@example.org", password_hash="x",
                    email_verified=True, blocked=True))
        p = Project(org_id=org.id, name="Mailer", description="d", kind="ai",
                    speciality="general-webapp")
        db.add(p)
        db.commit()
        ids = {"org": org.id, "project": p.id, "owner": owner.email.lower(),
               "member": member.email.lower(), "tag": tag}
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(Message).where(Message.project_id == ids["project"]))
            db.execute(delete(ProjectToolConfig).where(
                ProjectToolConfig.project_id == ids["project"]))
            db.execute(delete(Project).where(Project.id == ids["project"]))
            db.execute(delete(User).where(User.org_id == ids["org"]))
            db.execute(delete(Organization).where(Organization.id == ids["org"]))
            db.commit()


# ── seeding ──────────────────────────────────────────────────────────────

def test_seeded_disabled_and_pointed_at_the_sidecar():
    with SyncSession() as db:
        t = _row(db)
        assert t.kind == "email" and t.enabled is False and t.api_key_enc is None
        assert t.url.endswith("/email/mcp")
        assert mcp_names.tool_server_name(t) == "email"
        assert email_tool.is_configured(t) is False


def test_seeding_twice_makes_one_row():
    seed_tools()
    with SyncSession() as db:
        assert len(db.execute(select(Tool).where(Tool.kind == "email")).scalars().all()) == 1


# ── addresses and tokens ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Ops@Example.com", "ops@example.com"),
    ("  a.b+tag@sub.example.org ", "a.b+tag@sub.example.org"),
    ("a@b.c, evil@x.y", None),         # a second recipient smuggled in
    ("a@example.com\nBcc: x@y.z", None),  # a header smuggled in
    ("Name <a@example.com>", None),
    ("not-an-address", None),
    ("a@localhost", None),
    ("", None),
])
def test_only_one_plain_address_normalizes(raw, expected):
    assert email_tool.normalize_address(raw) == expected


def test_token_round_trips_to_its_project():
    tok = email_tool.mint_token("proj-1", 600)
    assert email_tool.read_token(tok) == "proj-1"


def test_expired_token_is_refused():
    assert email_tool.read_token(email_tool.mint_token("proj-1", -1)) is None


def test_a_tampered_token_is_refused():
    tag, payload, sig = email_tool.mint_token("proj-1", 600).split(".")
    other = email_tool._b64(f"proj-2:{int(time.time()) + 600}".encode())
    assert email_tool.read_token(f"{tag}.{other}.{sig}") is None
    assert email_tool.read_token(f"{tag}.{payload}.{sig[:-2]}AA") is None


@pytest.mark.parametrize("junk", [None, "", "x", "et1.a.b", "bwk_abc", "et2.e30.e30", "a.b.c.d"])
def test_junk_is_not_a_token(junk):
    assert email_tool.read_token(junk) is None


# ── what reaches a build ─────────────────────────────────────────────────

def test_a_build_gets_a_project_token_never_the_password(project):
    _configure(password="hunter2-smtp")
    with SyncSession() as db:
        p = db.get(Project, project["project"])
        cfg, secrets = tasks._mcp_config(db, project=p)
    entry = json.loads(cfg)["mcpServers"]["email"]
    assert entry["url"].endswith("/email/mcp")
    token = entry["headers"]["Authorization"].removeprefix("Bearer ")
    assert email_tool.read_token(token) == project["project"]
    assert "hunter2-smtp" not in cfg and "hunter2-smtp" not in secrets
    # The token joins the leak-scan refuse-set like any credential.
    assert token in secrets


def test_the_token_outlives_the_run_and_not_much_more(project):
    _configure()
    with SyncSession() as db:
        p = db.get(Project, project["project"])
        token = next(k for slug, _, k in tasks._project_tools(db, p) if slug == "email")
        timeout_s = tasks._run_timeout_minutes(p) * 60
    exp = int(email_tool._unb64(token.split(".")[1]).decode().rsplit(":", 1)[1])
    assert timeout_s < exp - time.time() <= timeout_s + email_tool.TOKEN_MARGIN_S


def test_no_project_no_email_server():
    _configure()
    with SyncSession() as db:
        assert "email" not in json.loads(tasks._mcp_config(db)[0])["mcpServers"]


def test_an_unconfigured_row_never_reaches_a_build(project):
    with SyncSession() as db:
        _row(db).enabled = True
        db.commit()
        p = db.get(Project, project["project"])
        assert "email" not in json.loads(tasks._mcp_config(db, project=p)[0])["mcpServers"]


def test_a_project_can_turn_it_off(project):
    tool_id = _configure()
    with SyncSession() as db:
        db.add(ProjectToolConfig(project_id=project["project"], tool_id=tool_id, enabled=False))
        db.commit()
        p = db.get(Project, project["project"])
        assert "email" not in json.loads(tasks._mcp_config(db, project=p)[0])["mcpServers"]


# ── sending ──────────────────────────────────────────────────────────────

class FakeSMTP:
    """Records the conversation instead of having one."""
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(email_tool.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_tool.smtplib, "SMTP_SSL", type("FakeSSL", (FakeSMTP,), {}))
    return FakeSMTP


def test_send_uses_starttls_and_the_stored_password(fake_smtp):
    _configure(password="pw-1")
    with SyncSession() as db:
        email_tool.send(_row(db), ["a@example.com", "b@example.com"],
                        "Build done\r\nBcc: evil@x.y", "It shipped.")
    conn = fake_smtp.instances[0]
    assert (conn.host, conn.port) == ("smtp.example.com", 587)
    assert conn.calls[:4] == ["ehlo", "starttls", "ehlo", ("login", "mailer", "pw-1")]
    msg = conn.sent[0]
    assert msg["To"] == "a@example.com, b@example.com"
    assert msg["From"] == "Builds <notify@example.com>"
    # The line break is collapsed: no header can be smuggled through the subject.
    assert msg["Subject"] == "Build done Bcc: evil@x.y" and msg["Bcc"] is None


def test_ssl_mode_opens_an_implicit_tls_connection(fake_smtp):
    _configure()
    with SyncSession() as db:
        t = _row(db)
        t.params = {**t.params, "smtp": {**t.params["smtp"], "security": "ssl", "port": 465}}
        db.commit()
        email_tool.send(_row(db), ["a@example.com"], "s", "b")
    conn = fake_smtp.instances[0]
    assert type(conn).__name__ == "FakeSSL" and "starttls" not in conn.calls


def test_an_smtp_failure_is_a_readable_error_without_the_password(monkeypatch):
    _configure(password="pw-secret")

    class Refusing(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    monkeypatch.setattr(email_tool.smtplib, "SMTP", Refusing)
    with SyncSession() as db:
        with pytest.raises(email_tool.EmailToolError) as exc:
            email_tool.send(_row(db), ["a@example.com"], "s", "b")
        ok, detail = email_tool.probe(_row(db))
    assert "username or password" in str(exc.value) and "pw-secret" not in str(exc.value)
    assert ok is False and "username or password" in detail


# ── the admin API ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    import asyncio

    from fastapi.testclient import TestClient

    from app.core.db import engine
    from app.main import app
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    r = events.get_sync_redis()
    for k in r.scan_iter("rl:*"):
        r.delete(k)
    with TestClient(app) as c:
        yield c
    # The pooled connections are bound to this module's loop: abandon them on
    # the way out, or the next HTTP module inherits them closed.
    asyncio.run(engine.dispose(close=False))
    events._async_client = None


@pytest.fixture
def admin(client):
    from app.core.config import settings
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login",
                    json={"email": settings.admin_email, "password": settings.admin_password},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def _tool_id():
    with SyncSession() as db:
        return _row(db).id


def test_settings_save_and_the_password_never_comes_back(client, admin):
    out = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin, json={"email": {
        "smtp_host": "smtp.example.com", "smtp_port": 2525, "smtp_security": "starttls",
        "smtp_username": "mailer", "smtp_password": "pw-xyz",
        "from_address": "Notify@Example.com", "from_name": " Builds\n",
        "allowed_recipients": ["Ops@example.com", "ops@example.com", "", "cto@example.org"]}})
    assert out.status_code == 200, out.text
    body = out.json()
    assert "pw-xyz" not in out.text
    assert body["has_api_key"] is False
    assert body["email"] == {
        "smtp_host": "smtp.example.com", "smtp_port": 2525, "smtp_security": "starttls",
        "smtp_username": "mailer", "has_password": True,
        "from_address": "notify@example.com", "from_name": "Builds",
        "allowed_recipients": ["ops@example.com", "cto@example.org"], "configured": True}
    with SyncSession() as db:
        t = _row(db)
        assert t.api_key_enc is None and t.enabled is False
        assert decrypt(t.params["smtp"]["password_enc"]) == "pw-xyz"
    # An empty password clears it; omitted fields stay.
    body = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin,
                        json={"email": {"smtp_password": ""}}).json()["email"]
    assert body["has_password"] is False and body["smtp_host"] == "smtp.example.com"


@pytest.mark.parametrize("patch,needle", [
    ({"from_address": "two@a.com, three@b.com"}, "Not an email address"),
    ({"allowed_recipients": ["ok@example.com", "Name <x@y.z>"]}, "Not an email address"),
    ({"smtp_host": "smtp.example.com/evil"}, "hostname"),
])
def test_bad_settings_are_refused(client, admin, patch, needle):
    r = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin, json={"email": patch})
    assert r.status_code == 422 and needle in r.text


def test_the_email_row_takes_no_api_key_and_others_take_no_email_settings(client, admin):
    r = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin, json={"api_key": "k"})
    assert r.status_code == 422
    with SyncSession() as db:
        gh = db.execute(select(Tool).where(Tool.slug == "github")).scalar_one().id
    r = client.patch(f"/api/admin/tools/{gh}", headers=admin,
                     json={"email": {"smtp_host": "x.example.com"}})
    assert r.status_code == 422


def test_enabling_proves_the_smtp_login(client, admin, monkeypatch):
    r = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin, json={"enabled": True})
    assert r.status_code == 409 and "SMTP host" in r.json()["detail"]

    _configure(enabled=False)
    monkeypatch.setattr(email_tool, "probe", lambda t: (False, "the SMTP server rejected it"))
    r = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin, json={"enabled": True})
    assert r.status_code == 409 and "rejected" in r.json()["detail"]
    with SyncSession() as db:
        assert _row(db).enabled is False

    monkeypatch.setattr(email_tool, "probe", lambda t: (True, ""))
    r = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin, json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True


def test_an_smtp_change_on_an_enabled_row_is_reproven(client, admin, monkeypatch):
    _configure(enabled=True)
    probed = []
    monkeypatch.setattr(email_tool, "probe", lambda t: (probed.append(1), (False, "nope"))[1])
    r = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin,
                     json={"email": {"smtp_host": "other.example.com"}})
    assert r.status_code == 409 and probed and r.json()["detail"].startswith("SMTP settings not saved")
    with SyncSession() as db:
        assert _row(db).params["smtp"]["host"] == "smtp.example.com"  # not persisted
    # The allowlist is not an SMTP setting: no login needed to edit it.
    probed.clear()
    r = client.patch(f"/api/admin/tools/{_tool_id()}", headers=admin,
                     json={"email": {"allowed_recipients": ["x@example.com"]}})
    assert r.status_code == 200 and not probed


def test_verify_reports_the_smtp_login(client, admin, monkeypatch):
    _configure(enabled=False)
    monkeypatch.setattr(email_tool, "probe", lambda t: (False, "could not reach the SMTP server"))
    out = client.post(f"/api/admin/tools/{_tool_id()}/verify", headers=admin).json()
    assert out["ok"] is False and out["detail"].startswith("SMTP:")
    monkeypatch.setattr(email_tool, "probe", lambda t: (True, ""))
    out = client.post(f"/api/admin/tools/{_tool_id()}/verify", headers=admin).json()
    assert out["ok"] is True and "SMTP login works" in out["detail"]


def test_test_email_goes_to_the_calling_admin(client, admin, monkeypatch):
    from app.core.config import settings
    _configure(enabled=False)
    sent = []
    monkeypatch.setattr(email_tool, "send", lambda t, to, subject, body: sent.append(to))
    out = client.post(f"/api/admin/tools/{_tool_id()}/test-email", headers=admin).json()
    assert out["ok"] is True and sent == [[settings.admin_email.lower()]]


def test_projects_override_only_the_enable_state(client, admin, project, monkeypatch):
    _configure(enabled=False)
    url = f"/api/admin/projects/{project['project']}/tools/{_tool_id()}"
    assert client.put(url, headers=admin, json={"url": "http://evil.example.com/mcp"}).status_code == 422
    assert client.put(url, headers=admin, json={"api_key": "k"}).status_code == 422
    monkeypatch.setattr(email_tool, "probe", lambda t: (False, "nope"))
    assert client.put(url, headers=admin, json={"enabled": True, "url": ""}).status_code == 409
    monkeypatch.setattr(email_tool, "probe", lambda t: (True, ""))
    assert client.put(url, headers=admin, json={"enabled": True, "url": ""}).status_code == 200


# ── the internal route the sidecar relays to ─────────────────────────────

def _bearer(project_id):
    return {"Authorization": f"Bearer {email_tool.mint_token(project_id, 600)}"}


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(email_tool, "send",
                        lambda t, to, subject, body: sent.append((to, subject, body)))
    return sent


def test_internal_routes_are_not_under_api():
    # Traefik and the SPA's nginx forward /api and /ws only: /internal stays
    # reachable from the internal network and nowhere else.
    from app.api.email_tool import router
    assert all(r.path.startswith("/internal/") for r in router.routes)


def test_without_a_valid_token_nothing_happens(client, project, outbox):
    _configure()
    for headers in ({}, {"Authorization": "Bearer junk"},
                    {"Authorization": f"Bearer {email_tool.mint_token(project['project'], -5)}"},
                    {"Authorization": f"Bearer {email_tool.mint_token(str(uuid.uuid4()), 600)}"}):
        r = client.post("/internal/email-tool/send", headers=headers,
                        json={"to": [project["owner"]], "subject": "s", "body": "b"})
        assert r.status_code == 401
    assert outbox == []


def test_recipients_are_the_customer_and_the_admin_list(client, project):
    _configure(allowed=["ops@example.com", project["owner"]])
    out = client.get("/internal/email-tool/recipients", headers=_bearer(project["project"]))
    assert out.status_code == 200
    assert out.json()["recipients"] == [
        {"address": project["owner"], "label": "project customer"},
        {"address": project["member"], "label": "project customer"},
        {"address": "ops@example.com", "label": "approved by the administrator"}]


def test_an_approved_send_goes_out_and_is_recorded_in_chat(client, project, outbox):
    _configure(allowed=["ops@example.com"])
    r = client.post("/internal/email-tool/send", headers=_bearer(project["project"]),
                    json={"to": [project["owner"].upper(), "ops@example.com", "ops@example.com"],
                          "subject": "Your build\nis done", "body": "All green."})
    assert r.status_code == 200, r.text
    assert r.json()["sent_to"] == [project["owner"], "ops@example.com"]
    (to, subject, body), = outbox
    assert to == [project["owner"], "ops@example.com"] and subject == "Your build is done"
    assert body.startswith("All green.") and "Mailer" in body
    with SyncSession() as db:
        notes = db.execute(select(Message).where(
            Message.project_id == project["project"])).scalars().all()
    assert len(notes) == 1 and notes[0].author == "system"
    assert "ops@example.com" in notes[0].body and "Your build is done" in notes[0].body


def test_unverified_and_blocked_accounts_are_not_customers(client, project, outbox):
    _configure()
    for who in ("unverified", "blocked"):
        r = client.post("/internal/email-tool/send", headers=_bearer(project["project"]),
                        json={"to": [f"{who}-{project['tag']}@example.org"],
                              "subject": "s", "body": "b"})
        assert r.status_code == 403
    assert outbox == []


def test_an_unapproved_recipient_blocks_the_whole_send(client, project, outbox):
    _configure()
    r = client.post("/internal/email-tool/send", headers=_bearer(project["project"]),
                    json={"to": [project["owner"], "stranger@example.net"],
                          "subject": "s", "body": "b"})
    assert r.status_code == 403
    assert "stranger@example.net" in r.json()["detail"] and "list_recipients" in r.json()["detail"]
    assert outbox == []


def test_a_disabled_tool_stops_a_running_build(client, project, outbox):
    tool_id = _configure()
    headers = _bearer(project["project"])
    with SyncSession() as db:
        db.add(ProjectToolConfig(project_id=project["project"], tool_id=tool_id, enabled=False))
        db.commit()
    r = client.post("/internal/email-tool/send", headers=headers,
                    json={"to": [project["owner"]], "subject": "s", "body": "b"})
    assert r.status_code == 403
    with SyncSession() as db:
        db.execute(delete(ProjectToolConfig))
        _row(db).enabled = False
        db.commit()
    r = client.get("/internal/email-tool/recipients", headers=headers)
    assert r.status_code == 403 and outbox == []


def test_the_hourly_quota_is_per_project(client, project, outbox, monkeypatch):
    _configure()
    monkeypatch.setattr(email_tool, "SENDS_PER_HOUR", 2)
    headers = _bearer(project["project"])
    codes = [client.post("/internal/email-tool/send", headers=headers,
                         json={"to": [project["owner"]], "subject": "s", "body": "b"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429] and len(outbox) == 2


def test_an_smtp_failure_reaches_the_agent_as_a_reason(client, project, monkeypatch):
    _configure()

    def boom(*a, **kw):
        raise email_tool.EmailToolError("could not reach the SMTP server (TimeoutError)")

    monkeypatch.setattr(email_tool, "send", boom)
    r = client.post("/internal/email-tool/send", headers=_bearer(project["project"]),
                    json={"to": [project["owner"]], "subject": "s", "body": "b"})
    assert r.status_code == 502 and "could not reach the SMTP server" in r.json()["detail"]
    with SyncSession() as db:
        assert not db.execute(select(Message).where(
            Message.project_id == project["project"])).scalars().all()
