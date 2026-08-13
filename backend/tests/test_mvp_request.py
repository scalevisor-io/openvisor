"""§threads Request #0: every ai-kind project is born with its initial build as
a server-created `mvp` Request, so the whole build conversation lives in that
request's thread and main stays the orchestrator. Covers creation (customer API
path), the _dev_thread routing + pre-threads fallback, the start/handle guards,
and the delivery-approval close.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import (
    CreditTransaction, Message, Organization, Project, Request, StatusChange,
    User,
)
from app.services import events
from app.workers import tasks

_ORG_IDS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    with SyncSession() as db:
        pids = db.execute(select(Project.id)
                          .where(Project.org_id.in_(_ORG_IDS or ["-"]))).scalars().all()
        if pids:
            db.execute(delete(Message).where(Message.project_id.in_(pids)))
            db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
            db.execute(delete(Request).where(Request.project_id.in_(pids)))
        db.execute(delete(CreditTransaction).where(CreditTransaction.org_id.in_(_ORG_IDS or ["-"])))
        db.execute(delete(User).where(User.org_id.in_(_ORG_IDS or ["-"])))
        db.execute(delete(Project).where(Project.org_id.in_(_ORG_IDS or ["-"])))
        db.execute(delete(Organization).where(Organization.id.in_(_ORG_IDS or ["-"])))
        db.commit()


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _login(client):
    # The login limiter (20/900s per IP) is shared by every module in a full
    # run; this module's extra logins must not tip later modules over it.
    try:
        events.get_sync_redis().delete("rl:login:testclient")
    except Exception:
        pass
    email = f"mvpreq-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "mvpreq-secret-1"
    with SyncSession() as db:
        org = Organization(name="MvpReq HTTP Org", credit_balance=100.0)
        db.add(org)
        db.flush()
        _ORG_IDS.append(org.id)
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        db.commit()
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def _create_ai_project(client, h) -> str:
    r = client.post("/api/projects", json={
        "kind": "ai", "speciality": "general-webapp",
        "description": "A todo app with CSV exports.", "from_scratch": True,
        "sovereign": False}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_ai_project_born_with_mvp_request(client, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    pid = _create_ai_project(client, _login(client))
    with SyncSession() as db:
        req = db.execute(select(Request).filter_by(project_id=pid, type="mvp")
                         ).scalars().one()
        assert (req.status, req.handling, req.title) == ("open", "ai", "Initial build")
        seed = db.execute(select(Message)
                          .filter_by(project_id=pid, thread=f"request:{req.id}")
                          ).scalars().all()
        assert [m.author for m in seed] == ["customer"]
        assert "todo app" in seed[0].body
        # the MVP build narrates into Request #0's thread
        p = db.get(Project, pid)
        assert tasks._dev_thread(db, p) == f"request:{req.id}"


def test_dev_thread_scoped_wins_and_legacy_falls_back_to_main():
    with SyncSession() as db:
        org = Organization(name="MvpReq Thread Org")
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai",
                    status="development")
        db.add(p)
        db.flush()
        # pre-threads project (no mvp row): narration stays in main
        assert tasks._dev_thread(db, p) == "main"
        mvp = Request(project_id=p.id, type="mvp", title="Initial build")
        scoped = Request(project_id=p.id, type="feature", title="Exports")
        db.add_all([mvp, scoped])
        db.flush()
        assert tasks._dev_thread(db, p) == f"request:{mvp.id}"
        # a request-scoped run always narrates into ITS request's thread
        p.dev_request_id = scoped.id
        assert tasks._dev_thread(db, p) == f"request:{scoped.id}"
        db.rollback()


def test_start_and_handle_refuse_the_mvp_request(client, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    monkeypatch.setattr(events, "publish_sync", lambda *a, **k: None)
    h = _login(client)
    pid = _create_ai_project(client, h)
    with SyncSession() as db:
        rid = db.execute(select(Request.id).filter_by(project_id=pid, type="mvp")
                         ).scalars().one()
    r = client.post(f"/api/projects/{pid}/requests/{rid}/start", headers=h)
    assert r.status_code == 409
    assert "Resume development" in r.json()["detail"]
    # the worker guard: a stray handle_request dispatch is a silent no-op
    before = _msg_count(pid)
    tasks.handle_request(pid, rid, "")
    with SyncSession() as db:
        status = db.execute(select(Request.status).filter_by(id=rid)).scalar_one()
    assert status == "open" and _msg_count(pid) == before


def _msg_count(pid: str) -> int:
    with SyncSession() as db:
        return len(db.execute(select(Message.id).filter_by(project_id=pid)
                              ).scalars().all())


def test_approve_delivery_closes_request_zero(client, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    h = _login(client)
    pid = _create_ai_project(client, h)
    with SyncSession() as db:
        p = db.get(Project, pid)
        p.status = "awaiting_customer"
        p.demo_deployed_once = True
        db.commit()
    r = client.post(f"/api/projects/{pid}/approve-delivery", headers=h)
    assert r.status_code == 200, r.text
    with SyncSession() as db:
        status = db.execute(select(Request.status)
                            .filter_by(project_id=pid, type="mvp")).scalar_one()
        assert status == "done"
