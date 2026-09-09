"""§20 review gate: what an admin's move INTO `development` does while
`block_auto_development` is still set.

A `review_required` evaluation blocks the project and a human authorizes it by
clearing the flag. Moving the STATUS is not that authorization: the dispatch is
skipped, not deferred, so the project lands in `development` with no run and
none coming. The skip was silent - no task, no log, no message - which left the
absence of a task as its only evidence while the customer's panel promised a
build "in a moment" (prod: a blocked project priced and advanced by hand sat
that way, its initial-build thread reading "Development is queued." for as long
as anyone looked). These pin both sides of the guard and the warning that makes
the skip readable in the logs.
"""
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import (
    DevRun, Message, Organization, Project, Request, StatusChange, User,
)
from app.workers.celery_app import celery

RUN_DEVELOPMENT = "app.workers.tasks.run_development"


@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="Blocked Dev Org", credit_balance=100.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(
                Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(
                    StatusChange.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    # Same async-pool healing as the other HTTP test modules (test_dev_runs_api).
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    events.get_sync_redis().delete("rl:login:testclient")
    with TestClient(app) as c:
        yield c
    events.get_sync_redis().delete("rl:login:testclient")


@pytest.fixture
def sent(monkeypatch):
    """Every Celery task name the request dispatches. transition_async's emails
    ride the SAME app object as the dev kick, so patch the instance rather than
    one module's import - otherwise the kick escapes the capture."""
    names: list[str] = []
    monkeypatch.setattr(celery, "send_task",
                        lambda name, *a, **k: names.append(name))
    return names


def _admin_and_project(org_id, *, blocked: bool):
    email = f"blockdev-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "admin-secret-123"
    with SyncSession() as db:
        db.add(User(org_id=org_id, email=email, password_hash=hash_password(pwd),
                    role="admin", email_verified=True))
        p = Project(org_id=org_id, name="P", description="d", kind="ai",
                    status="payment_due", block_auto_development=blocked)
        db.add(p)
        db.flush()
        db.add(Request(project_id=p.id, type="mvp", handling="ai",
                       status="open", title="Initial build"))
        db.commit()
        return email, pwd, p.id


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def _set_status(client, headers, pid, status):
    return client.post(f"/api/admin/projects/{pid}/status", headers=headers,
                       json={"status": status})


def test_blocked_project_advances_without_dispatching_a_build(client, org, sent,
                                                              caplog):
    email, pwd, pid = _admin_and_project(org, blocked=True)
    headers = _auth(client, email, pwd)

    with caplog.at_level(logging.WARNING, logger="app.api.admin"):
        r = _set_status(client, headers, pid, "development")
    assert r.status_code == 200, r.text

    with SyncSession() as db:
        # The status move itself is the admin's to make and still stands...
        assert db.get(Project, pid).status == "development"
        # ...but nothing was dispatched and no slot was taken, so the ledger has
        # no row a panel could read as a build in flight.
        assert db.query(DevRun).filter_by(project_id=pid).count() == 0
    assert RUN_DEVELOPMENT not in sent
    # The skip is now readable: an operator asking "why is nothing building?"
    # finds the answer in the API log instead of inferring it from silence.
    assert any(pid in rec.getMessage() and "blocked" in rec.getMessage()
               for rec in caplog.records), caplog.text


def test_unblocked_project_still_kicks_the_pipeline(client, org, sent):
    email, pwd, pid = _admin_and_project(org, blocked=False)
    headers = _auth(client, email, pwd)

    r = _set_status(client, headers, pid, "development")
    assert r.status_code == 200, r.text

    assert RUN_DEVELOPMENT in sent
    with SyncSession() as db:
        row = db.query(DevRun).filter_by(project_id=pid).one()
        assert row.state == "queued"  # the slot the dispatch rides on
