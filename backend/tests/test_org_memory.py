"""Global (organization-scoped) Memory: the dev-pipeline merge (§ global memory) -
project memory + org memory when enabled, project keys overriding global keys, the
per-project override vs org default, and the token fallback - plus one HTTP pass over
the /api/org-memory + per-project /memory/settings surface (scoping + toggles).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import decrypt, encrypt
from app.core.security import hash_password
from app.main import app
from app.models import (
    CreditTransaction, Message, Organization, OrgMemory, Project, ProjectMemory,
    ProjectRepo, StatusChange, User,
)
from app.workers import tasks


@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="Global-mem Org", credit_balance=10.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
                db.execute(delete(ProjectMemory).where(ProjectMemory.project_id.in_(pids)))
            db.execute(delete(OrgMemory).where(OrgMemory.org_id == oid))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _project(oid, *, use_global=None, proj_mem=(), org_mem=(), org_default=True):
    """proj_mem/org_mem = [(key, value, is_secret)]. Returns the project id."""
    with SyncSession() as db:
        db.get(Organization, oid).global_memory_enabled_default = org_default
        p = Project(org_id=oid, name="P", description="d", kind="ai",
                    status="development", use_global_memory=use_global)
        db.add(p)
        db.flush()
        for k, v, s in proj_mem:
            db.add(ProjectMemory(project_id=p.id, author="customer", key=k,
                                 value_enc=encrypt(v), is_secret=s))
        for k, v, s in org_mem:
            # org memory is unique per (org, key); upsert so a test can build several
            # projects under one org fixture without colliding on a shared global key.
            row = db.execute(select(OrgMemory).where(
                OrgMemory.org_id == oid, OrgMemory.key == k)).scalar_one_or_none()
            if row:
                row.value_enc, row.is_secret = encrypt(v), s
            else:
                db.add(OrgMemory(org_id=oid, author="customer", key=k,
                                 value_enc=encrypt(v), is_secret=s))
        db.commit()
        return p.id


def _mem_map(db, pid):
    p = db.get(Project, pid)
    return {m.key: decrypt(m.value_enc) for m in tasks._effective_memory(db, p)}


# ---------------------------------------------------------------- merge semantics

def test_merge_includes_global_when_enabled(org):
    pid = _project(org, proj_mem=[("A", "pa", False)], org_mem=[("B", "gb", False)])
    with SyncSession() as db:
        assert _mem_map(db, pid) == {"A": "pa", "B": "gb"}


def test_project_key_overrides_global(org):
    pid = _project(org, proj_mem=[("K", "project", False)], org_mem=[("K", "global", False)])
    with SyncSession() as db:
        assert _mem_map(db, pid) == {"K": "project"}


def test_org_default_off_excludes_global(org):
    pid = _project(org, org_default=False, proj_mem=[("A", "pa", False)],
                   org_mem=[("B", "gb", False)])
    with SyncSession() as db:
        assert _mem_map(db, pid) == {"A": "pa"}  # global not pulled in


def test_project_override_beats_org_default(org):
    # org default OFF but the project explicitly opts IN → global included
    pid = _project(org, org_default=False, use_global=True, org_mem=[("B", "gb", False)])
    with SyncSession() as db:
        assert _mem_map(db, pid) == {"B": "gb"}
    # org default ON but the project explicitly opts OUT → global excluded
    pid2 = _project(org, org_default=True, use_global=False, org_mem=[("B", "gb", False)])
    with SyncSession() as db:
        assert _mem_map(db, pid2) == {}


def test_secret_global_entry_is_secret(org):
    pid = _project(org, org_mem=[("SECRET_KEY", "s3cr3t", True)])
    with SyncSession() as db:
        rows = tasks._effective_memory(db, db.get(Project, pid))
        secret = [m for m in rows if m.is_secret]
        assert len(secret) == 1 and decrypt(secret[0].value_enc) == "s3cr3t"


def test_token_falls_back_to_global(org, monkeypatch):
    monkeypatch.setattr(tasks.settings, "github_token", "")
    pid = _project(org, org_mem=[("GITHUB_TOKEN", "ghp_global", True)])
    with SyncSession() as db:
        assert tasks._project_repo_token(db, db.get(Project, pid), "github") == "ghp_global"
    # opted out → no global token, and no platform fallback configured → None
    pid2 = _project(org, use_global=False, org_mem=[("GITHUB_TOKEN", "ghp_global", True)])
    with SyncSession() as db:
        assert tasks._project_repo_token(db, db.get(Project, pid2), "github") is None


def test_project_token_wins_over_global(org, monkeypatch):
    monkeypatch.setattr(tasks.settings, "github_token", "")
    pid = _project(org, proj_mem=[("GITHUB_TOKEN", "ghp_project", True)],
                   org_mem=[("GITHUB_TOKEN", "ghp_global", True)])
    with SyncSession() as db:
        assert tasks._project_repo_token(db, db.get(Project, pid), "github") == "ghp_project"


# ---------------------------------------------------------------- HTTP surface

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    # Heal both module-level async pools an earlier HTTP module left bound to its
    # now-closed loop: the SQLAlchemy engine pool, and the async Redis client (the
    # login rate limiter). Dropping the singleton makes it lazily recreated in this
    # module's TestClient loop; otherwise a login here raises "got Future attached to
    # a different loop". (This module sorts last among the HTTP test modules.)
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _customer(org_id):
    email = f"gmem-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "customer-secret-123"
    with SyncSession() as db:
        db.add(User(org_id=org_id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        p = Project(org_id=org_id, name="P", description="d", kind="ai", status="draft")
        db.add(p)
        db.commit()
        return email, pwd, p.id


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_org_memory_requires_auth(client):
    assert client.get("/api/org-memory").status_code in (401, 403)


def test_org_memory_crud_and_settings(org, client):
    email, pwd, pid = _customer(org)
    h = _auth(client, email, pwd)

    # default settings: enabled
    assert client.get("/api/org-memory/settings", headers=h).json() == {"enabled_default": True}
    # flip the org default off
    r = client.put("/api/org-memory/settings", json={"enabled_default": False}, headers=h)
    assert r.status_code == 200 and r.json()["enabled_default"] is False

    # upsert an org memory entry, then it lists
    r = client.put("/api/org-memory",
                   json={"key": "AWS_KEY", "value": "v", "is_secret": True, "description": "d"},
                   headers=h)
    assert r.status_code == 200, r.text
    entry_id = r.json()["id"]
    rows = client.get("/api/org-memory", headers=h).json()
    assert [e["key"] for e in rows] == ["AWS_KEY"]
    assert rows[0]["value"] == "v"  # returned in clear like project memory

    # per-project settings reflect the org default (off) with no override
    s = client.get(f"/api/projects/{pid}/memory/settings", headers=h).json()
    assert s == {"use_global_memory": None, "org_default": False, "effective": False}
    # explicit per-project override ON beats the org default
    s = client.put(f"/api/projects/{pid}/memory/settings",
                   json={"use_global_memory": True}, headers=h).json()
    assert s == {"use_global_memory": True, "org_default": False, "effective": True}
    # reset to inherit
    s = client.put(f"/api/projects/{pid}/memory/settings",
                   json={"use_global_memory": None}, headers=h).json()
    assert s["use_global_memory"] is None and s["effective"] is False

    # delete
    assert client.delete(f"/api/org-memory/{entry_id}", headers=h).status_code == 200
    assert client.get("/api/org-memory", headers=h).json() == []


def test_org_memory_scoped_to_own_org(org, client):
    """A user can't delete another org's global memory entry."""
    email, pwd, _ = _customer(org)
    h = _auth(client, email, pwd)
    with SyncSession() as db:
        other = Organization(name="Other gmem org")
        db.add(other)
        db.flush()
        foreign = OrgMemory(org_id=other.id, author="customer", key="X", value_enc=encrypt("y"))
        db.add(foreign)
        db.commit()
        foreign_id, other_id = foreign.id, other.id
    try:
        assert client.delete(f"/api/org-memory/{foreign_id}", headers=h).status_code == 404
    finally:
        with SyncSession() as db:
            db.execute(delete(OrgMemory).where(OrgMemory.org_id == other_id))
            db.execute(delete(Organization).where(Organization.id == other_id))
            db.commit()
