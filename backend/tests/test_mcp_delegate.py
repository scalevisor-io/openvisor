"""§MCP delegate: handing work to the build pipeline from someone's terminal.

The tool wraps project_actions, so the §12/§14 guards are already covered
elsewhere; what needs pinning here is the layer this adds - a project-scoped
token requirement, a repository to publish into, a wallet floor, and the daily
cap that keeps a credential living in a terminal from spending build-sized money
in a loop.
"""
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import new_api_token
from app.models import (ApiToken, CreditTransaction, Message, Organization, Project,
                        ProjectRepo, Request, StatusChange, User, utcnow)


@pytest.fixture
def seeded():
    with SyncSession() as db:
        org = Organization(name="Delegate Org", credit_balance=50.0)
        db.add(org)
        db.commit()
        user = User(org_id=org.id, email=f"del-{org.id[:8]}@example.org",
                    password_hash="x", role="customer", email_verified=True)
        db.add(user)
        p = Project(org_id=org.id, name="Widgets", description="policy", kind="auto_dev",
                    status="development", workspace_path="/tmp/del")
        db.add(p)
        db.commit()
        db.add(ProjectRepo(project_id=p.id, ssh_uri="git@github.com:acme/widgets.git",
                           provider="github", role="main", is_push_target=True))
        db.commit()
        ids = (org.id, user.id, p.id)
    try:
        yield ids
    finally:
        with SyncSession() as db:
            oid, uid, pid = ids
            db.execute(delete(ApiToken).where(ApiToken.user_id == uid))
            db.execute(delete(Message).where(Message.project_id == pid))
            db.execute(delete(StatusChange).where(StatusChange.project_id == pid))
            db.execute(delete(Request).where(Request.project_id == pid))
            db.execute(delete(ProjectRepo).where(ProjectRepo.project_id == pid))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.id == pid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _mint(user_id: str, scope: str, project_id: str | None = None) -> str:
    plaintext, token_hash = new_api_token()
    with SyncSession() as db:
        db.add(ApiToken(user_id=user_id, token_hash=token_hash, name=scope, scope=scope,
                        project_id=project_id))
        db.commit()
    return plaintext


@pytest.fixture(scope="module")
def client():
    # Entered as a context manager so every request shares ONE persistent event
    # loop; otherwise the module-level async engine caches asyncpg connections
    # across TestClient's per-request loops ("Event loop is closed") - the same
    # trap test_hub.py documents.
    import asyncio

    from fastapi.testclient import TestClient
    from app.core.db import engine
    from app.main import app

    # An earlier test module ran the shared async engine in ITS event loop and
    # left pooled asyncpg connections behind; they can't be used from this
    # module's loop. Drop them first, then let TestClient open fresh ones.
    asyncio.run(engine.dispose())
    with TestClient(app) as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_delegation_files_and_starts_a_request(client, seeded, monkeypatch):
    """The happy path: one Request, AI-handled, dispatched - and the caller gets
    back what it needs to follow the work without reading the thread."""
    from app.workers import celery_app

    _, uid, pid = seeded
    sent: list = []
    monkeypatch.setattr(celery_app.celery, "send_task",
                        lambda name, args=None, **k: sent.append((name, args)))

    r = client.post("/api/mcp/delegate",
                    json={"spec": "Add a CSV export to the reports page.", "type": "feature"},
                    headers=_auth(_mint(uid, "project", pid)))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "open" and body["type"] == "feature"
    assert body["url"].endswith(f"/projects/{pid}/requests/{body['request_id']}")
    assert body["pull_requests"] == []

    with SyncSession() as db:
        req = db.get(Request, body["request_id"])
        assert req.handling == "ai" and req.project_id == pid
        # the spec is the work order - it IS kept, as the thread's first message
        msg = db.execute(select(Message).where(
            Message.thread == f"request:{req.id}")).scalars().first()
        assert "CSV export" in msg.body
    assert any(name.endswith("handle_request") for name, _ in sent), sent


def test_a_plain_user_token_cannot_delegate(client, seeded):
    """Delegation spends build money against ONE project - an org-wide token has
    no project to charge or build in."""
    _, uid, _ = seeded
    r = client.post("/api/mcp/delegate", json={"spec": "do a thing"},
                    headers=_auth(_mint(uid, "user")))
    assert r.status_code == 403
    assert "project-scoped" in r.json()["detail"]


def test_hub_token_is_refused(client, seeded):
    _, uid, _ = seeded
    r = client.post("/api/mcp/delegate", json={"spec": "do a thing"},
                    headers=_auth(_mint(uid, "hub")))
    assert r.status_code == 403


def test_no_repository_is_refused_before_spending(client, seeded):
    """A delegation's deliverable is a pull request; without somewhere to push,
    refusing up front beats a run that dies at publish time."""
    _, uid, pid = seeded
    with SyncSession() as db:
        db.execute(delete(ProjectRepo).where(ProjectRepo.project_id == pid))
        db.commit()
    r = client.post("/api/mcp/delegate", json={"spec": "x"},
                    headers=_auth(_mint(uid, "project", pid)))
    assert r.status_code == 409
    assert "no repository" in r.json()["detail"]
    with SyncSession() as db:
        assert db.execute(select(Request).where(Request.project_id == pid)
                          ).scalars().first() is None


def test_empty_wallet_is_refused(client, seeded):
    oid, uid, pid = seeded
    with SyncSession() as db:
        db.get(Organization, oid).credit_balance = 0.0
        db.commit()
    r = client.post("/api/mcp/delegate", json={"spec": "x"},
                    headers=_auth(_mint(uid, "project", pid)))
    assert r.status_code == 402


def test_daily_cap_stops_a_runaway_terminal(client, seeded, monkeypatch):
    from app.core.config import settings
    from app.workers import celery_app

    _, uid, pid = seeded
    monkeypatch.setattr(settings, "mcp_delegate_daily_max", 2)
    monkeypatch.setattr(celery_app.celery, "send_task", lambda *a, **k: None)
    h = _auth(_mint(uid, "project", pid))

    codes = [client.post("/api/mcp/delegate", json={"spec": f"task {i}"}, headers=h).status_code
             for i in range(3)]
    assert codes == [201, 201, 429], codes

    # yesterday's work never counts against today
    with SyncSession() as db:
        for req in db.execute(select(Request).where(Request.project_id == pid)).scalars():
            req.created_at = utcnow() - timedelta(days=2)
        db.commit()
    assert client.post("/api/mcp/delegate", json={"spec": "fresh day"},
                       headers=h).status_code == 201


def test_delegations_are_listed_and_fetched_without_the_thread(client, seeded, monkeypatch):
    from app.workers import celery_app

    _, uid, pid = seeded
    monkeypatch.setattr(celery_app.celery, "send_task", lambda *a, **k: None)
    h = _auth(_mint(uid, "project", pid))
    rid = client.post("/api/mcp/delegate", json={"spec": "Add gravity"},
                      headers=h).json()["request_id"]

    one = client.get(f"/api/mcp/delegations/{rid}", headers=h).json()
    assert one["request_id"] == rid
    assert "spec" not in one and "messages" not in one  # the thread stays on-platform

    listed = client.get("/api/mcp/delegations", headers=h).json()["delegations"]
    assert [d["request_id"] for d in listed] == [rid]

    # a delegation id from another project is a 404, never a cross-project read
    with SyncSession() as db:
        other = Project(org_id=db.get(Project, pid).org_id, name="Other", description="d",
                        kind="ai", status="development", workspace_path="/tmp/o")
        db.add(other)
        db.commit()
        foreign = Request(project_id=other.id, type="feature", handling="ai", title="theirs")
        db.add(foreign)
        db.commit()
        fid, oid2 = foreign.id, other.id
    assert client.get(f"/api/mcp/delegations/{fid}", headers=h).status_code == 404
    with SyncSession() as db:
        db.execute(delete(Request).where(Request.project_id == oid2))
        db.execute(delete(Project).where(Project.id == oid2))
        db.commit()


# ---------------------------------------------------------------- consult (1b)

def test_consult_queues_a_readonly_run_and_stores_nothing(client, seeded, monkeypatch):
    """A consult dispatches the harness read-only and answers out of band; the
    question must not touch the database."""
    import json as _json

    from app.services import events
    from app.workers import celery_app

    _, uid, pid = seeded
    sent: list = []
    monkeypatch.setattr(celery_app.celery, "send_task",
                        lambda name, args=None, **k: sent.append((name, args)))

    class _Redis:
        store: dict = {}

        def set(self, k, v, ex=None):
            self.store[k] = v

        def get(self, k):
            return self.store.get(k)

    fake = _Redis()
    monkeypatch.setattr(events, "get_sync_redis", lambda: fake)

    secret_q = "why does the payment retry loop double-charge on 502"
    h = _auth(_mint(uid, "project", pid))
    r = client.post("/api/mcp/consult", json={"question": secret_q}, headers=h)
    assert r.status_code == 202, r.text
    job = r.json()["job_id"]
    assert r.json()["state"] == "queued"
    assert any(n.endswith("run_mcp_consult") for n, _ in sent)

    # polling reflects redis, and the question is nowhere in the DB
    fake.store[f"mcpconsult:{job}"] = _json.dumps({"state": "done", "answer": "It retries."})
    got = client.get(f"/api/mcp/consult/{job}", headers=h).json()
    assert got["state"] == "done" and got["answer"] == "It retries."

    with SyncSession() as db:
        rows = db.execute(select(Message.body).where(Message.project_id == pid)).scalars().all()
    assert all(secret_q not in (b or "") for b in rows), "a consult question must not be stored"


def test_consult_needs_a_project_token(client, seeded, monkeypatch):
    _, uid, _ = seeded
    r = client.post("/api/mcp/consult", json={"question": "how does auth work"},
                    headers=_auth(_mint(uid, "user")))
    assert r.status_code == 403


def test_unknown_consult_job_is_404(client, seeded, monkeypatch):
    from app.services import events

    class _Redis:
        def get(self, k):
            return None

        def set(self, k, v, ex=None):
            return True

    monkeypatch.setattr(events, "get_sync_redis", lambda: _Redis())
    _, uid, pid = seeded
    r = client.get("/api/mcp/consult/nope", headers=_auth(_mint(uid, "project", pid)))
    assert r.status_code == 404
