"""§sharing: share a project with a registered user as contributor or read-only.
Covers the share-management surface (owner-org only, create-or-update, target
validation), the access it grants (list/search visibility with `access`, project
GET, contributor mutations), the viewer method-gate (every mutation 403s), and
revocation."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import (
    Message, Organization, Project, ProjectShare, StatusChange, User,
)


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    # Heal the module-level async pools a prior HTTP test module left bound to
    # its now-closed loop (same dance as the other HTTP test modules).
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    # This module re-logs-in on every actor switch (one shared cookie jar), which
    # would blow the 20/900s per-IP login budget the rest of the suite also draws
    # from - reset the shared counter so neither side starves the other.
    events.get_sync_redis().delete("rl:login:testclient")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def world():
    """Two orgs: the owning org (user + project) and a stranger org (user)."""
    pwd = "sharing-secret-123"
    tag = uuid.uuid4().hex[:8]
    with SyncSession() as db:
        owner_org = Organization(name="Share-owner Org", credit_balance=10.0)
        other_org = Organization(name="Share-target Org", credit_balance=0.0)
        db.add_all([owner_org, other_org])
        db.flush()
        owner = User(org_id=owner_org.id, email=f"share-owner-{tag}@example.com",
                     password_hash=hash_password(pwd), role="customer", email_verified=True)
        teammate = User(org_id=owner_org.id, email=f"share-teammate-{tag}@example.com",
                        password_hash=hash_password(pwd), role="customer", email_verified=True)
        target = User(org_id=other_org.id, email=f"share-target-{tag}@example.com",
                      password_hash=hash_password(pwd), role="customer", email_verified=True)
        project = Project(org_id=owner_org.id, name="Shared thing", description="d",
                          kind="ai", status="development")
        db.add_all([owner, teammate, target, project])
        db.commit()
        ids = {"pwd": pwd, "owner": owner.email, "teammate": teammate.email,
               "target": target.email, "pid": project.id,
               "org_ids": [owner_org.id, other_org.id]}
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(ProjectShare).where(ProjectShare.project_id == ids["pid"]))
            db.execute(delete(Message).where(Message.project_id == ids["pid"]))
            db.execute(delete(StatusChange).where(StatusChange.project_id == ids["pid"]))
            db.execute(delete(Project).where(Project.id == ids["pid"]))
            db.execute(delete(User).where(User.org_id.in_(ids["org_ids"])))
            db.execute(delete(Organization).where(Organization.id.in_(ids["org_ids"])))
            db.commit()


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def _share(client, h, pid, email, role):
    return client.post(f"/api/projects/{pid}/shares",
                       json={"email": email, "role": role}, headers=h)


def test_unshared_project_is_invisible(client, world):
    h = _auth(client, world["target"], world["pwd"])
    assert client.get(f"/api/projects/{world['pid']}", headers=h).status_code == 404
    assert world["pid"] not in [p["id"] for p in client.get("/api/projects", headers=h).json()]


def test_share_target_validation(client, world):
    h = _auth(client, world["owner"], world["pwd"])
    pid = world["pid"]
    r = _share(client, h, pid, f"nobody-{uuid.uuid4().hex[:8]}@example.com", "viewer")
    assert r.status_code == 404
    # a user of the owning org already has access
    assert _share(client, h, pid, world["teammate"], "viewer").status_code == 400
    # the instance admin already sees every project
    with SyncSession() as db:
        admin_email = db.execute(select(User.email).where(User.role == "admin")).scalars().first()
    if admin_email:
        assert _share(client, h, pid, admin_email, "viewer").status_code == 400


def test_viewer_sees_everything_and_touches_nothing(client, world):
    pid = world["pid"]
    h_owner = _auth(client, world["owner"], world["pwd"])
    r = _share(client, h_owner, pid, world["target"].upper(), "viewer")  # case-insensitive
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "viewer" and r.json()["email"] == world["target"]

    h = _auth(client, world["target"], world["pwd"])
    # visible in the list (+ search scope) with the caller's access role
    listed = {p["id"]: p for p in client.get("/api/projects", headers=h).json()}
    assert listed[pid]["access"] == "viewer"
    searched = client.get("/api/projects/search?q=", headers=h).json()["results"]
    assert pid in [p["id"] for p in searched]
    # full read surface
    assert client.get(f"/api/projects/{pid}", headers=h).json()["access"] == "viewer"
    assert client.get(f"/api/projects/{pid}/messages", headers=h).status_code == 200
    assert client.get(f"/api/projects/{pid}/requests", headers=h).status_code == 200
    # every mutation is method-gated
    for call in (
        lambda: client.post(f"/api/projects/{pid}/messages",
                            json={"thread": "main", "body": "hi"}, headers=h),
        lambda: client.patch(f"/api/projects/{pid}", json={"name": "Nope"}, headers=h),
        lambda: client.post(f"/api/projects/{pid}/submit", headers=h),
        lambda: client.post(f"/api/projects/{pid}/demo/start", headers=h),
        lambda: client.put(f"/api/projects/{pid}/memory",
                           json={"key": "K", "value": "v", "is_secret": False}, headers=h),
    ):
        r = call()
        assert r.status_code == 403, r.text
        assert "read-only" in r.json()["detail"]


def test_contributor_acts_as_customer(client, world):
    pid = world["pid"]
    h_owner = _auth(client, world["owner"], world["pwd"])
    # create-or-update: re-posting the same email changes the role
    assert _share(client, h_owner, pid, world["target"], "contributor").status_code == 201
    rows = client.get(f"/api/projects/{pid}/shares", headers=h_owner).json()
    assert [(s["email"], s["role"]) for s in rows] == [(world["target"], "contributor")]

    h = _auth(client, world["target"], world["pwd"])
    r = client.post(f"/api/projects/{pid}/messages",
                    json={"thread": "main", "body": "hello from a contributor"}, headers=h)
    assert r.status_code == 201 and r.json()["author"] == "customer"
    assert client.patch(f"/api/projects/{pid}", json={"name": "Renamed by contributor"},
                        headers=h).status_code == 200
    # ...but can never manage shares
    assert client.get(f"/api/projects/{pid}/shares", headers=h).status_code == 403
    assert _share(client, h, pid, world["teammate"], "viewer").status_code == 403


def test_revocation_ends_access(client, world):
    # The TestClient has ONE cookie jar, so each actor switch re-logs-in.
    pid = world["pid"]
    h_owner = _auth(client, world["owner"], world["pwd"])
    assert _share(client, h_owner, pid, world["target"], "viewer").status_code == 201
    share_id = client.get(f"/api/projects/{pid}/shares", headers=h_owner).json()[0]["id"]
    h = _auth(client, world["target"], world["pwd"])
    assert client.get(f"/api/projects/{pid}", headers=h).status_code == 200
    h_owner = _auth(client, world["owner"], world["pwd"])
    r = client.delete(f"/api/projects/{pid}/shares/{share_id}", headers=h_owner)
    assert r.status_code == 200
    h = _auth(client, world["target"], world["pwd"])
    assert client.get(f"/api/projects/{pid}", headers=h).status_code == 404


def test_share_add_is_rate_limited_including_misses(client, world, monkeypatch):
    """The 404-vs-201 answer confirms email existence, so share creation is
    capped per user - and probing unknown emails burns the same budget."""
    from app.api import projects as projects_api
    monkeypatch.setattr(projects_api.settings, "share_rate_per_hour", 3)
    h = _auth(client, world["owner"], world["pwd"])
    pid = world["pid"]
    for i in range(3):
        r = _share(client, h, pid, f"probe-{i}-{uuid.uuid4().hex[:6]}@example.com", "viewer")
        assert r.status_code == 404
    r = _share(client, h, pid, world["target"], "viewer")
    assert r.status_code == 429
