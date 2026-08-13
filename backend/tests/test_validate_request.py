"""§requests validate + cancel: the customer (or admin) closes an AI-handled
request by hand - validate marks it DELIVERED (the per-request twin of
approve_delivery), cancel closes it REJECTED and ends its work unit. Covers
the shared guards (mvp / manual / closed / live build), both happy paths
(run rows closed, mirror settled or work unit retracted, system paper
trail), and the mirror repointing at a still-active sibling.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, Request,
    StatusChange, User,
)
from app.services import events

_ORG_IDS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    with SyncSession() as db:
        pids = db.execute(select(Project.id)
                          .where(Project.org_id.in_(_ORG_IDS or ["-"]))).scalars().all()
        if pids:
            db.execute(update(Project).where(Project.id.in_(pids))
                       .values(dev_request_id=None))
            db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
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
    try:
        events.get_sync_redis().delete("rl:login:testclient")
    except Exception:
        pass
    email = f"validate-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "validate-secret-1"
    with SyncSession() as db:
        org = Organization(name="Validate HTTP Org", credit_balance=100.0)
        db.add(org)
        db.flush()
        _ORG_IDS.append(org.id)
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        p = Project(org_id=org.id, name="P", description="d", kind="ai",
                    status="development")
        db.add(p)
        db.commit()
        pid = p.id
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}, pid


def _request(pid, **kw):
    kw.setdefault("type", "bug")
    kw.setdefault("handling", "ai")
    kw.setdefault("status", "in_progress")
    kw.setdefault("title", "Fix CI env variables")
    with SyncSession() as db:
        req = Request(project_id=pid, **kw)
        db.add(req)
        db.commit()
        return req.id


def test_validate_guards(client):
    h, pid = _login(client)
    mvp = _request(pid, type="mvp", status="open", title="Initial build")
    assert client.post(f"/api/projects/{pid}/requests/{mvp}/validate",
                       headers=h).status_code == 409
    manual = _request(pid, handling="manual")
    assert client.post(f"/api/projects/{pid}/requests/{manual}/validate",
                       headers=h).status_code == 409
    closed = _request(pid, status="done")
    assert client.post(f"/api/projects/{pid}/requests/{closed}/validate",
                       headers=h).status_code == 409
    live = _request(pid)
    with SyncSession() as db:
        db.add(DevRun(project_id=pid, request_id=live, state="running",
                      workspace_dir=f"devruns/{pid}/x",
                      started_at=datetime.now(timezone.utc)))
        db.commit()
    r = client.post(f"/api/projects/{pid}/requests/{live}/validate", headers=h)
    assert r.status_code == 409 and "stop it first" in r.json()["detail"]


def test_validate_closes_request_run_and_mirror(client):
    """The prod regression shape: the run pushed onto an already-open PR, died,
    and parked - but here it sits awaiting_merge; the human declares the work
    delivered. Request done, row done, mirror settled, paper trail posted."""
    h, pid = _login(client)
    rid = _request(pid)
    with SyncSession() as db:
        db.add(DevRun(project_id=pid, request_id=rid, state="awaiting_merge",
                      workspace_dir="", branch="f/#2451-synchro-vecmilmaps",
                      pr_number=2453, started_at=datetime.now(timezone.utc)))
        p = db.get(Project, pid)
        p.dev_request_id = rid
        p.dev_run_state = "awaiting_merge"
        db.commit()
    r = client.post(f"/api/projects/{pid}/requests/{rid}/validate", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"
    with SyncSession() as db:
        row = db.execute(select(DevRun).filter_by(request_id=rid)).scalars().one()
        assert row.state == "done"
        assert db.get(Project, pid).dev_run_state == "done"
        trail = db.execute(select(Message).filter_by(
            project_id=pid, thread=f"request:{rid}", author="system")).scalars().all()
        assert any("validated as delivered" in m.body for m in trail)


def test_validate_repoints_mirror_at_a_live_sibling(client):
    h, pid = _login(client)
    rid = _request(pid)
    with SyncSession() as db:
        sib_req = Request(project_id=pid, type="feature", handling="ai",
                          status="in_progress", title="Sibling")
        db.add(sib_req)
        db.flush()
        db.add(DevRun(project_id=pid, request_id=rid, state="awaiting_merge",
                      workspace_dir="", started_at=datetime.now(timezone.utc)))
        db.add(DevRun(project_id=pid, request_id=sib_req.id, state="running",
                      workspace_dir=f"devruns/{pid}/sib", branch="agent/sibling",
                      started_at=datetime.now(timezone.utc)))
        p = db.get(Project, pid)
        p.dev_request_id = rid
        p.dev_run_state = "awaiting_merge"
        db.commit()
        sid = sib_req.id
    r = client.post(f"/api/projects/{pid}/requests/{rid}/validate", headers=h)
    assert r.status_code == 200, r.text
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "running"
        assert p.dev_request_id == sid
        assert p.dev_branch == "agent/sibling"


# ------------------------------------------------------------------ cancel

def test_cancel_guards(client):
    h, pid = _login(client)
    mvp = _request(pid, type="mvp", status="open", title="Initial build")
    assert client.post(f"/api/projects/{pid}/requests/{mvp}/cancel",
                       headers=h).status_code == 409
    closed = _request(pid, status="rejected")
    assert client.post(f"/api/projects/{pid}/requests/{closed}/cancel",
                       headers=h).status_code == 409
    live = _request(pid)
    with SyncSession() as db:
        db.add(DevRun(project_id=pid, request_id=live, state="running",
                      workspace_dir=f"devruns/{pid}/y",
                      started_at=datetime.now(timezone.utc)))
        db.commit()
    r = client.post(f"/api/projects/{pid}/requests/{live}/cancel", headers=h)
    assert r.status_code == 409 and "stop it first" in r.json()["detail"]


def test_cancel_rejects_request_and_ends_the_work_unit(client):
    """Canceling the mirror's own awaiting-merge request parks its run failed
    ('Request canceled'), clears the work-unit pointers and retracts the
    console (idle) - nothing is watched or resumable anymore."""
    h, pid = _login(client)
    rid = _request(pid)
    with SyncSession() as db:
        db.add(DevRun(project_id=pid, request_id=rid, state="awaiting_merge",
                      workspace_dir="", branch="agent/edit",
                      pr_number=9, started_at=datetime.now(timezone.utc)))
        p = db.get(Project, pid)
        p.dev_request_id = rid
        p.dev_run_state = "awaiting_merge"
        p.dev_branch = "agent/edit"
        p.dev_pr_number = 9
        db.commit()
    r = client.post(f"/api/projects/{pid}/requests/{rid}/cancel", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    with SyncSession() as db:
        row = db.execute(select(DevRun).filter_by(request_id=rid)).scalars().one()
        assert row.state == "failed" and "canceled" in row.run_error.lower()
        p = db.get(Project, pid)
        assert p.dev_run_state == "idle"
        assert p.dev_request_id is None and p.dev_branch is None
        assert p.dev_pr_number is None
        trail = db.execute(select(Message).filter_by(
            project_id=pid, thread=f"request:{rid}", author="system")).scalars().all()
        assert any("canceled" in m.body.lower() for m in trail)


def test_cancel_a_proposed_request_unblocks_the_next_proposal(client):
    """A proposed request (never built) cancels clean - and frees the
    classifier's one-proposal-at-a-time slot. §12 one-click dismiss: declining
    the PROPOSAL posts the canned "Not now" reply + the agent's drop ack to the
    main thread, so the proposal ack panel freezes (shared-ui confirmState)."""
    h, pid = _login(client)
    rid = _request(pid, status="proposed")
    r = client.post(f"/api/projects/{pid}/requests/{rid}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    with SyncSession() as db:
        main = db.execute(select(Message).filter_by(
            project_id=pid, thread="main").order_by(Message.created_at)).scalars().all()
        assert [m.body for m in main if m.author == "customer"] == ["Not now"]
        assert any(m.author == "agent" and "dropped" in m.body for m in main)


def test_start_a_proposed_request_posts_the_confirm_trail(client, monkeypatch):
    """§12 one-click confirm: starting a PROPOSED request (the chat ✓ button or
    the Requests tab) posts the canned "Go ahead" reply + the agent's dispatch
    ack with the request deep link to the main thread - the same trail the
    classifier's confirm verdict leaves - and dispatches handle_request.
    Starting an already-OPEN request (allowed: a re-dispatch) posts nothing."""
    from app.services import project_actions
    sent: list = []
    monkeypatch.setattr(project_actions.celery, "send_task",
                        lambda name, args=None, **k: sent.append((name, args)))
    h, pid = _login(client)
    rid = _request(pid, status="proposed", title="Add CSV export")
    r = client.post(f"/api/projects/{pid}/requests/{rid}/start", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"
    assert sent == [("app.workers.tasks.handle_request", [pid, rid, ""])]
    with SyncSession() as db:
        main = db.execute(select(Message).filter_by(
            project_id=pid, thread="main").order_by(Message.created_at)).scalars().all()
        assert [m.body for m in main if m.author == "customer"] == ["Go ahead"]
        acks = [m for m in main if m.author == "agent"]
        assert len(acks) == 1 and "Add CSV export" in acks[0].body
        assert f"/projects/{pid}/requests/{rid}" in acks[0].body
    # the request is OPEN now - a re-start is allowed (re-dispatch) but is not
    # a go-ahead moment, so it posts no further narration
    assert client.post(f"/api/projects/{pid}/requests/{rid}/start",
                       headers=h).status_code == 200
    with SyncSession() as db:
        again = db.execute(select(Message).filter_by(
            project_id=pid, thread="main")).scalars().all()
        assert len(again) == 2
