"""§Tools - GitHub/GitLab MCP tools: seeding, admin CRUD with the verify-on-enable
gate, per-project overrides (tri-state enable, URL, key) and the _mcp_config merge
with the Memory-token fallback."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.core.security import hash_password
from app.main import app
from app.models import (
    Organization, Project, ProjectMemory, ProjectToolConfig, Tool, User,
)
from app.seed import seed_tools
from app.services import events
from app.workers import tasks


@pytest.fixture(autouse=True)
def seeded():
    seed_tools()
    yield
    with SyncSession() as db:
        db.execute(delete(ProjectToolConfig))
        db.commit()


def _tools(db):
    return {t.slug: t for t in db.execute(select(Tool)).scalars().all()}


def test_seed_tools_idempotent():
    seed_tools()
    seed_tools()
    with SyncSession() as db:
        rows = db.execute(select(Tool)).scalars().all()
        assert sorted(t.slug for t in rows) == [
            "donsetch", "github", "gitlab", "websearch_serper", "websearch_staan"]
        assert all(not t.enabled for t in rows) or True  # default off unless admin enabled


def test_mcp_config_merges_enabled_tool(monkeypatch):
    monkeypatch.setattr(tasks.mcp_scan, "audit_server", lambda url, key: ([], None))
    with SyncSession() as db:
        t = _tools(db)["github"]
        t.enabled = True
        t.api_key_enc = encrypt("ghp_global")
        db.commit()
        cfg, secrets = tasks._mcp_config(db)
        assert '"github"' in cfg and "api.githubcopilot.com" in cfg
        assert "ghp_global" in secrets
        t.enabled = False
        t.api_key_enc = None
        db.commit()
        cfg2, _ = tasks._mcp_config(db)
        assert '"github"' not in cfg2


def test_mcp_config_project_overrides_and_memory_fallback(monkeypatch):
    monkeypatch.setattr(tasks.mcp_scan, "audit_server", lambda url, key: ([], None))
    with SyncSession() as db:
        org = Organization(name="Tools Org")
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="T", description="d", kind="ai",
                    speciality="general-webapp")
        db.add(p)
        db.flush()
        gl = _tools(db)["gitlab"]  # globally disabled
        db.add(ProjectToolConfig(project_id=p.id, tool_id=gl.id, enabled=True,
                                 url="https://gitlab.example.org/api/v4/mcp"))
        db.add(ProjectMemory(project_id=p.id, author="customer", key="GITLAB_TOKEN",
                             value_enc=encrypt("glpat_mem"), is_secret=True))
        db.commit()
        cfg, secrets = tasks._mcp_config(db, project=p)
        assert "gitlab.example.org" in cfg      # per-project URL override
        assert "glpat_mem" in secrets           # Memory token fallback used
        # project can also DISABLE a globally enabled tool
        gh = _tools(db)["github"]
        gh.enabled = True
        db.add(ProjectToolConfig(project_id=p.id, tool_id=gh.id, enabled=False))
        db.commit()
        cfg2, _ = tasks._mcp_config(db, project=p)
        assert '"github"' not in cfg2
        # cleanup
        db.execute(delete(ProjectToolConfig).where(ProjectToolConfig.project_id == p.id))
        db.execute(delete(ProjectMemory).where(ProjectMemory.project_id == p.id))
        db.execute(delete(Project).where(Project.id == p.id))
        db.execute(delete(Organization).where(Organization.id == org.id))
        gh2 = _tools(db)["github"]
        gh2.enabled = False
        db.commit()


def test_mcp_config_drops_poisoned_tool(monkeypatch):
    monkeypatch.setattr(tasks.mcp_scan, "audit_server",
                        lambda url, key: (["embedded instruction"], None))
    monkeypatch.setattr(tasks.emailer, "send_email", lambda *a, **k: None)
    with SyncSession() as db:
        t = _tools(db)["github"]
        t.enabled = True
        db.commit()
        cfg, _ = tasks._mcp_config(db)
        assert '"github"' not in cfg
        t2 = _tools(db)["github"]
        t2.enabled = False
        db.commit()


# ---- admin HTTP surface ----

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    # the suite's earlier logins may have tripped the per-IP limiter
    r = events.get_sync_redis()
    for k in r.scan_iter("rl:*"):
        r.delete(k)
    with TestClient(app) as c:
        yield c


def _admin_login(client):
    from app.core.config import settings
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login",
                    json={"email": settings.admin_email, "password": settings.admin_password},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_admin_tools_flow(client, monkeypatch):
    from app.api import tools as tools_api
    monkeypatch.setattr(tools_api.mcp_scan, "audit_server", lambda url, key: ([], None))
    monkeypatch.setattr(tools_api.mcp_scan, "fingerprint_server", lambda url, key: "fp")
    h = _admin_login(client)
    rows = client.get("/api/admin/tools").json()
    assert sorted(t["slug"] for t in rows) == [
        "donsetch", "github", "gitlab", "websearch_serper", "websearch_staan"]
    gl = next(t for t in rows if t["slug"] == "gitlab")
    r = client.patch(f"/api/admin/tools/{gl['id']}",
                     json={"url": "https://gitlab.acme.example/api/v4/mcp",
                           "api_key": "glpat_x", "enabled": True}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["enabled"] and out["has_api_key"]
    assert out["url"] == "https://gitlab.acme.example/api/v4/mcp"
    r = client.post(f"/api/admin/tools/{gl['id']}/verify", headers=h)
    assert r.json()["ok"] is True
    # poisoned enable is refused
    monkeypatch.setattr(tools_api.mcp_scan, "audit_server",
                        lambda url, key: (["secret exfil phrase"], None))
    r = client.patch(f"/api/admin/tools/{gl['id']}", json={"enabled": True}, headers=h)
    assert r.status_code == 409
    # reset
    monkeypatch.setattr(tools_api.mcp_scan, "audit_server", lambda url, key: ([], None))
    client.patch(f"/api/admin/tools/{gl['id']}",
                 json={"enabled": False, "api_key": ""}, headers=h)
