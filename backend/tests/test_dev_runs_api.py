"""§threads run-history surface: GET /projects/{id}/dev-runs (per-request
DevRun ledger rows, newest first, legacy-feed attribution) and dev-logs?run_id=
(one ledger row's captured log instead of the project scalars).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import DevRun, Organization, Project, Request, User


@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="DevRuns Org", credit_balance=10.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
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
    # Same async-pool healing + login-limiter clearing as the other HTTP test
    # modules (test_project_files.py pattern).
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    events.get_sync_redis().delete("rl:login:testclient")
    with TestClient(app) as c:
        yield c
    events.get_sync_redis().delete("rl:login:testclient")


def _customer(org_id):
    email = f"devruns-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "customer-secret-123"
    with SyncSession() as db:
        db.add(User(org_id=org_id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        p = Project(org_id=org_id, name="P", description="d", kind="ai",
                    status="development")
        db.add(p)
        db.flush()
        r = Request(project_id=p.id, type="feature", handling="ai",
                    status="done", title="R")
        db.add(r)
        db.commit()
        return email, pwd, p.id, r.id


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text


def _run(pid, rid, **kw):
    kw.setdefault("state", "done")
    kw.setdefault("workspace_dir", "")
    with SyncSession() as db:
        row = DevRun(project_id=pid, request_id=rid, **kw)
        db.add(row)
        db.commit()
        return row.id


def test_dev_runs_lists_request_history_newest_first(client, org):
    email, pwd, pid, rid = _customer(org)
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    old = _run(pid, rid, started_at=t0, branch="agent/one", pr_number=1,
               pr_url="https://github.com/acme/app/pull/1", tokens_consumed=100,
               security_review={"verdict": "approve", "findings": []})
    new = _run(pid, rid, state="failed", started_at=t0 + timedelta(hours=1),
               run_error="boom")
    _auth(client, email, pwd)
    rows = client.get(f"/api/projects/{pid}/dev-runs?request_id={rid}").json()
    assert [r["id"] for r in rows] == [new, old]
    assert rows[0]["run_error"] == "boom" and rows[0]["state"] == "failed"
    assert rows[1]["pr_url"].endswith("/pull/1")
    assert rows[1]["security_review"]["verdict"] == "approve"
    assert rows[1]["tokens_consumed"] == 100
    # the shared legacy feed belongs to the newest STARTED row only
    assert rows[0]["has_feed"] is True
    assert rows[1]["has_feed"] is False


def test_dev_runs_request_filter_and_parallel_feed_ownership(client, org):
    email, pwd, pid, rid = _customer(org)
    with SyncSession() as db:
        other = Request(project_id=pid, type="bug", handling="ai",
                        status="done", title="R2")
        db.add(other)
        db.commit()
        rid2 = other.id
    mine = _run(pid, rid, started_at=datetime.now(timezone.utc))
    # a parallel-mode run owns its workspace feed even when it is not newest
    theirs = _run(pid, rid2, workspace_dir=f"devruns/{pid}/abc",
                  started_at=datetime.now(timezone.utc) - timedelta(hours=1))
    _auth(client, email, pwd)
    scoped = client.get(f"/api/projects/{pid}/dev-runs?request_id={rid}").json()
    assert [r["id"] for r in scoped] == [mine]
    everything = client.get(f"/api/projects/{pid}/dev-runs").json()
    assert {r["id"] for r in everything} == {mine, theirs}
    by_id = {r["id"]: r for r in everything}
    assert by_id[theirs]["has_feed"] is True  # its own workspace feed
    assert by_id[mine]["has_feed"] is True  # newest legacy row


def test_dev_logs_serves_one_ledger_row(client, org):
    email, pwd, pid, rid = _customer(org)
    run = _run(pid, rid, state="failed", run_error="exploded",
               run_log="tail of the captured log", pr_number=4,
               pr_url="https://github.com/acme/app/pull/4")
    _auth(client, email, pwd)
    body = client.get(f"/api/projects/{pid}/dev-logs?run_id={run}").json()
    assert body["log"] == "tail of the captured log"
    assert body["dev_run_state"] == "failed"
    assert body["dev_run_error"] == "exploded"
    assert body["dev_pr_number"] == 4
    # unknown / foreign run ids 404 instead of leaking another project's log
    assert client.get(
        f"/api/projects/{pid}/dev-logs?run_id={uuid.uuid4()}").status_code == 404
    _, _, pid2, rid2 = _customer(org)
    foreign = _run(pid2, rid2, run_log="not yours")
    assert client.get(
        f"/api/projects/{pid}/dev-logs?run_id={foreign}").status_code == 404


def test_project_detail_payload_carries_active_dev_runs(client, org):
    """§parallel-builds MR4: the detail payload's dev_runs[] - ACTIVE rows only,
    oldest started first, dev_run_out shape - feeds the stacked consoles."""
    email, pwd, pid, rid = _customer(org)
    t0 = datetime.now(timezone.utc)
    _run(pid, rid, state="done", started_at=t0 - timedelta(hours=3))  # terminal: absent
    with SyncSession() as db:
        r2 = Request(project_id=pid, type="bug", handling="ai",
                     status="in_progress", title="R2")
        db.add(r2)
        db.commit()
        rid2 = r2.id
    older = _run(pid, rid, state="awaiting_merge",
                 started_at=t0 - timedelta(hours=2), branch="agent/one")
    newer = _run(pid, rid2, state="running", workspace_dir=f"devruns/{pid}/n",
                 started_at=t0 - timedelta(hours=1))
    _auth(client, email, pwd)
    runs = client.get(f"/api/projects/{pid}").json()["dev_runs"]
    assert [r["id"] for r in runs] == [older, newer]
    assert runs[0]["state"] == "awaiting_merge" and runs[0]["branch"] == "agent/one"
    assert runs[1]["state"] == "running"
    # feed attribution matches /dev-runs: the newest-started legacy row owns
    # the project feed; a parallel row owns its workspace feed
    assert runs[0]["has_feed"] is True and runs[1]["has_feed"] is True


def test_stop_build_scoped_to_a_sibling_run(client, org, monkeypatch):
    """§parallel-builds MR4: ?run_id= stops ONE sibling - admitted even when
    the mirror scalar is not 'running' (the worker validates the row); the
    legacy no-run_id form keeps its mirror 409."""
    from app.services import project_actions
    sent = []
    monkeypatch.setattr(project_actions.celery, "send_task",
                        lambda name, args=None, **k: sent.append((name, args)))
    email, pwd, pid, rid = _customer(org)
    run = _run(pid, rid, state="running", workspace_dir=f"devruns/{pid}/x")
    _auth(client, email, pwd)
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post(f"/api/projects/{pid}/stop-build?run_id={run}",
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    assert sent == [("app.workers.tasks.stop_development", [pid, run])]
    r = client.post(f"/api/projects/{pid}/stop-build",
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 409  # mirror idle, no scoped run named
