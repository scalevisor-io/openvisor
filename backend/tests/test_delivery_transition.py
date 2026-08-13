"""§8 delivery hand-off: once an MVP demo is live, the project moves to
awaiting_customer so the customer can "Approve delivery". Regression for the
platform-GitLab happy path (auto-merge on green CI), which deployed the demo but
left the project in development - hiding the approve button and skipping the
delivery email. DB-backed in the test_build_control style (committed throwaway
org, tasks open their own sessions, redis via the running stack).
"""
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.models import (
    DeploymentEvent, Message, Organization, Project, Request, StatusChange,
)
from app.services import events
from app.workers import tasks


@pytest.fixture
def quiet(monkeypatch):
    """Capture WS events and detach demo_start from the broker, deployer and
    port allocator so it runs without external services."""
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_find_demo_dir", lambda w: "/tmp/demo")
    monkeypatch.setattr(tasks, "_allocate_port", lambda db, p: 20001)
    monkeypatch.setattr(tasks, "_htpasswd", lambda p: "")
    monkeypatch.setattr(tasks.deployer_client, "start_demo", lambda *a, **k: None)
    return ws


@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="Delivery Test Org", credit_balance=100.0)
        db.add(org)
        db.commit()
        oid = org.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(
                Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(DeploymentEvent).where(DeploymentEvent.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _commit_project(oid, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("workspace_path", "/tmp/ws")
    kw.setdefault("subdomain", "sub-" + uuid.uuid4().hex[:8])
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.commit()
        return p.id


def test_successful_mvp_deploy_hands_ball_to_customer(quiet, org_id):
    # platform-GitLab happy path: merge-success -> deploying -> demo_start finalize
    pid = _commit_project(org_id, status="development", dev_run_state="deploying")
    tasks.demo_start(pid, "start")
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.status == "awaiting_customer"   # §8: the customer can now Approve delivery
        assert p.dev_run_state == "done"
        assert p.demo_state == "running"


def test_request_delivery_does_not_change_project_status(quiet, org_id):
    # a scoped §14 request redeploy marks the request done but must not move the
    # project's lifecycle status
    pid = _commit_project(org_id, status="finished", dev_run_state="deploying")
    with SyncSession() as db:
        req = Request(project_id=pid, type="feature", title="add a button",
                      status="in_progress")
        db.add(req)
        p = db.get(Project, pid)
        p.dev_request_id = req.id
        db.commit()
        rid = req.id
    tasks.demo_start(pid, "start")
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.status == "finished"            # unchanged - request flow owns its own status
        assert p.dev_run_state == "done"
        assert p.dev_request_id is None
        assert db.get(Request, rid).status == "done"


def test_plain_restart_does_not_transition(quiet, org_id):
    # a dashboard demo restart (the run already finished) redeploys without a lifecycle move
    pid = _commit_project(org_id, status="finished", dev_run_state="done")
    tasks.demo_start(pid, "start")
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.status == "finished"            # unchanged
        assert p.dev_run_state == "done"
        assert p.demo_state == "running"
