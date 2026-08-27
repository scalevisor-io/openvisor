"""§Tools web search: the keyed providers after they moved off the KB page.

A knowledge base is a corpus the agent consults; a keyed SERP API is a
capability it has. These pin what must survive that move:

  - the run-facing server name (`websearch_serper`) - project instructions quote
    it, so the slug carries the prefix the KB name used to supply;
  - the never-trust-the-client key probe on enable, which used to live in
    `_update_websearch_kb` and now has to be re-run from the tools API;
  - "a keyless provider never reaches a build", which the KB dispatcher enforced
    by skipping keyless rows;
  - and the per-project gate, which INVERTED (opt-in selection → tri-state
    override) and is the whole reason the migration writes explicit rows.
"""
import json

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import decrypt, encrypt
from app.main import app
from app.models import Organization, Project, ProjectToolConfig, Tool
from app.seed import seed_tools
from app.services import events, mcp_names, websearch
from app.workers import tasks


def _reset():
    with SyncSession() as db:
        db.execute(delete(ProjectToolConfig))
        for t in db.execute(select(Tool).where(Tool.kind == websearch.KIND)).scalars():
            t.enabled = False
            t.api_key_enc = None
        db.commit()


@pytest.fixture(autouse=True)
def seeded():
    seed_tools()
    _reset()
    yield
    _reset()


def _row(db, provider="serper") -> Tool:
    return db.execute(select(Tool).where(
        Tool.slug == websearch.tool_slug(provider))).scalar_one()


@pytest.fixture(autouse=True)
def _clean_scan(monkeypatch):
    from app.services import mcp_scan
    monkeypatch.setattr(mcp_scan, "audit_server", lambda url, api_key=None: ([], None))
    monkeypatch.setattr(tasks, "_vet_mcp_server", lambda *a, **kw: True)


# ── seeding ──────────────────────────────────────────────────────────────

def test_providers_are_seeded_as_disabled_tools():
    with SyncSession() as db:
        rows = db.execute(select(Tool).where(Tool.kind == websearch.KIND)).scalars().all()
        assert {t.slug for t in rows} == {"websearch_serper", "websearch_staan"}
        for t in rows:
            assert t.enabled is False and t.api_key_enc is None
            assert t.params["provider"] in websearch.PROVIDERS
            # Fixed per provider - unlike the donsetch row, nothing here moves.
            assert t.url.endswith(f"/{t.params['provider']}/mcp")


def test_the_run_facing_name_did_not_move():
    # The whole point of the `websearch_` slug prefix: an instruction that says
    # "use websearch_staan" keeps addressing the same server.
    with SyncSession() as db:
        assert mcp_names.tool_server_name(_row(db, "staan")) == "websearch_staan"
        assert mcp_names.tool_server_name(_row(db, "serper")) == "websearch_serper"


# ── what reaches a build ─────────────────────────────────────────────────

def test_enabled_provider_resolves_to_the_sidecar_with_its_bearer():
    with SyncSession() as db:
        t = _row(db)
        t.enabled = True
        t.api_key_enc = encrypt("serper-key-123")
        db.commit()
        cfg, secrets = tasks._mcp_config(db)
        entry = json.loads(cfg)["mcpServers"]["websearch_serper"]
        assert entry["url"].endswith("/serper/mcp")
        assert entry["headers"] == {"Authorization": "Bearer serper-key-123"}
        # The provider key joins the leak-scan refuse-set, as it did as a KB.
        assert "serper-key-123" in secrets


def test_keyless_provider_never_reaches_a_build():
    # The API keeps these disabled, but an enabled-then-cleared row must not be
    # injected either: it would only hand the agent a tool that always errors.
    with SyncSession() as db:
        t = _row(db)
        t.enabled = True
        t.api_key_enc = None
        db.commit()
        cfg, _ = tasks._mcp_config(db)
        assert "websearch_serper" not in json.loads(cfg)["mcpServers"]


def test_project_override_still_gates_it():
    """The gate INVERTED in the move (opt-in selection → tri-state override), so
    a project that must not search now says so with an explicit false."""
    with SyncSession() as db:
        org = Organization(name="WS Org")
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai",
                    speciality="general-webapp")
        db.add(p)
        db.flush()
        t = _row(db)
        t.enabled = True
        t.api_key_enc = encrypt("k")
        db.add(ProjectToolConfig(project_id=p.id, tool_id=t.id, enabled=False))
        db.commit()

        assert "websearch_serper" not in json.loads(tasks._mcp_config(db, project=p)[0])["mcpServers"]
        # ...and a project with no override inherits the global flag.
        assert "websearch_serper" in json.loads(tasks._mcp_config(db)[0])["mcpServers"]

        db.execute(delete(ProjectToolConfig).where(ProjectToolConfig.project_id == p.id))
        db.execute(delete(Project).where(Project.id == p.id))
        db.execute(delete(Organization).where(Organization.id == org.id))
        db.commit()


# ── the admin gate ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    import asyncio

    from fastapi.testclient import TestClient

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    r = events.get_sync_redis()
    for k in r.scan_iter("rl:*"):
        r.delete(k)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    from app.core.config import settings
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login",
                    json={"email": settings.admin_email, "password": settings.admin_password},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_admin_payload_exposes_the_provider(client, admin):
    rows = client.get("/api/admin/tools").json()
    row = next(r for r in rows if r["slug"] == "websearch_serper")
    assert row["kind"] == "websearch" and row["provider"] == "serper"
    assert row["has_api_key"] is False and row["enabled"] is False
    assert row["mcp_server"] == "websearch_serper"


def test_key_is_encrypted_at_rest_and_never_returned(client, admin):
    with SyncSession() as db:
        tool_id = _row(db).id
    out = client.patch(f"/api/admin/tools/{tool_id}",
                       json={"api_key": "serper-secret-1"}, headers=admin).json()
    assert out["has_api_key"] is True and "api_key" not in out
    # Storing a key does not turn the row on by itself.
    assert out["enabled"] is False
    with SyncSession() as db:
        row = db.get(Tool, tool_id)
        assert row.api_key_enc != "serper-secret-1"
        assert decrypt(row.api_key_enc) == "serper-secret-1"


def test_enable_reprobes_the_key_against_the_provider(client, admin, monkeypatch):
    monkeypatch.setattr("app.api.tools.mcp_scan.fingerprint_server", lambda url, key: "fp")
    with SyncSession() as db:
        tool_id = _row(db).id
    client.patch(f"/api/admin/tools/{tool_id}", json={"api_key": "k"}, headers=admin)

    monkeypatch.setattr("app.api.tools.websearch.verify_key",
                        lambda provider, key: (False, "the provider rejected this API key"))
    r = client.patch(f"/api/admin/tools/{tool_id}", json={"enabled": True}, headers=admin)
    assert r.status_code == 409 and "rejected" in r.json()["detail"]

    seen = {}

    def _ok(provider, key):
        seen["provider"], seen["key"] = provider, key
        return True, ""

    monkeypatch.setattr("app.api.tools.websearch.verify_key", _ok)
    r = client.patch(f"/api/admin/tools/{tool_id}", json={"enabled": True}, headers=admin)
    assert r.status_code == 200 and r.json()["enabled"] is True
    # The probe gets the row's provider and its STORED key, not anything a
    # client sent - the never-trust-the-client rule the KB page had.
    assert seen == {"provider": "serper", "key": "k"}


def test_enabling_without_a_key_is_refused_without_reaching_the_provider(client, admin, monkeypatch):
    def _boom(provider, key):
        assert not key, "a keyless enable must never send a request with no key"
        return False, "an API key is required"

    monkeypatch.setattr("app.api.tools.websearch.verify_key", _boom)
    with SyncSession() as db:
        tool_id = _row(db).id
    r = client.patch(f"/api/admin/tools/{tool_id}", json={"enabled": True}, headers=admin)
    assert r.status_code == 409


def test_clearing_the_key_force_disables(client, admin, monkeypatch):
    monkeypatch.setattr("app.api.tools.websearch.verify_key", lambda p, k: (True, ""))
    monkeypatch.setattr("app.api.tools.mcp_scan.fingerprint_server", lambda url, key: "fp")
    with SyncSession() as db:
        tool_id = _row(db).id
    client.patch(f"/api/admin/tools/{tool_id}", json={"api_key": "k"}, headers=admin)
    assert client.patch(f"/api/admin/tools/{tool_id}",
                        json={"enabled": True}, headers=admin).json()["enabled"] is True
    out = client.patch(f"/api/admin/tools/{tool_id}", json={"api_key": ""}, headers=admin).json()
    assert out["has_api_key"] is False and out["enabled"] is False
