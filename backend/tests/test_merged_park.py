"""§14 merged-work park semantics: a change that LANDED must never read as a
failed build. Prod regression (angry-birds request): the platform auto-merged
MR !3, the post-merge demo deploy died on the checkout sync, the run parked
'failed', and the customer's Resume burned a full rebuild only to conclude "no
changes to publish" - twice mislabeling a delivered request. Covers the
'merged' park, its finalization by the next demo start, and adopt probe 0
(_adopt_merged_change).
"""
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.models import (
    DeploymentEvent, DevRun, Message, Organization, Project, Request, StatusChange,
)
from app.services import dev_concurrency, events
from app.workers import tasks


@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="MergedPark Test Org", credit_balance=100.0)
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
                for p in db.execute(select(Project).where(
                        Project.id.in_(pids))).scalars().all():
                    p.dev_request_id = None  # FK to request rows deleted below
                db.flush()
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(DeploymentEvent).where(DeploymentEvent.project_id.in_(pids)))
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


@pytest.fixture
def quiet(monkeypatch):
    started: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: None)
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_find_demo_dir", lambda w: ".")
    monkeypatch.setattr(tasks, "_allocate_port", lambda db, p: 20003)
    monkeypatch.setattr(tasks, "_htpasswd", lambda p: "")
    monkeypatch.setattr(tasks.deployer_client, "start_demo",
                        lambda *a, **k: started.append(a))
    return started


def _project(oid, **kw):
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


def _request_and_run(pid, run_state, workspace_dir="", pr_number=None,
                     pr_url=None):
    with SyncSession() as db:
        req = Request(project_id=pid, type="bug", title="fix it",
                      status="in_progress")
        db.add(req)
        db.flush()
        run = DevRun(project_id=pid, request_id=req.id, state=run_state,
                     workspace_dir=workspace_dir, pr_number=pr_number,
                     pr_url=pr_url)
        db.add(run)
        p = db.get(Project, pid)
        p.dev_request_id = req.id
        db.commit()
        return req.id, run.id


def _target(remote="ssh://git@git.example.com:10022/grp/repo.git"):
    return {"remote": remote, "base_branch": "main", "provider": "gitlab"}


def _messages(pid):
    with SyncSession() as db:
        return [m.body for m in db.execute(
            select(Message).where(Message.project_id == pid)
            .order_by(Message.created_at)).scalars().all()]


def test_refresh_failure_parks_run_merged_not_failed(quiet, org_id, monkeypatch):
    pid = _project(org_id, dev_run_state="deploying")
    req_id, run_id = _request_and_run(pid, "deploying",
                                      workspace_dir=f"devruns/{pid}/r1")
    monkeypatch.setattr(tasks, "_dev_target", lambda db, p: _target())
    monkeypatch.setattr(tasks, "_refresh_root_workspace",
                        lambda db, p: (_ for _ in ()).throw(
                            RuntimeError("ssh: connect timed out")))
    tasks.demo_start(pid, "start", run_id=run_id)
    with SyncSession() as db:
        row = db.get(DevRun, run_id)
        p = db.get(Project, pid)
        assert row.state == "merged"                    # delivered, not failed
        assert "Demo deploy parked" in (row.run_error or "")
        assert p.dev_run_state == "merged"
        assert p.status == "awaiting_admin"
    assert quiet == []                                  # nothing was deployed
    assert any("merged change itself is safe" in m for m in _messages(pid))


def test_next_demo_start_finalizes_a_merged_row(quiet, org_id, monkeypatch):
    pid = _project(org_id, dev_run_state="merged")
    req_id, run_id = _request_and_run(pid, "merged",
                                      workspace_dir=f"devruns/{pid}/r1")
    monkeypatch.setattr(tasks, "_dev_target", lambda db, p: None)  # sync not under test
    tasks.demo_start(pid, "start")                      # manual restart, no run_id
    with SyncSession() as db:
        row = db.get(DevRun, run_id)
        p = db.get(Project, pid)
        req = db.get(Request, req_id)
        assert row.state == "done"
        assert row.run_error is None
        assert req.status == "done"
        assert p.dev_request_id is None
        assert p.dev_run_state == "done"
    assert len(quiet) == 1
    assert any("Request delivered" in m for m in _messages(pid))


def test_adopt_merged_change_delivers_instead_of_failing(quiet, org_id, monkeypatch):
    pid = _project(org_id, dev_run_state="running", gitlab_project_id=4242,
                   gitlab_web_url="https://gitlab.example.com/plat/proj")
    req_id, run_id = _request_and_run(pid, "running", pr_number=3)
    monkeypatch.setattr(tasks.gitlab, "get_mr",
                        lambda glid, num: {"state": "merged"})
    dispatched: list = []
    monkeypatch.setattr(tasks.demo_start, "apply_async",
                        lambda *a, **k: dispatched.append((a, k)))
    with SyncSession() as db:
        p = db.get(Project, pid)
        run = db.get(DevRun, run_id)
        dev_concurrency.bind_run(p, run)
        assert tasks._adopt_merged_change(db, p, "main", "final logs") is True
        db.commit()
    with SyncSession() as db:
        p = db.get(Project, pid)
        run = db.get(DevRun, run_id)
        req = db.get(Request, req_id)
        assert p.dev_run_state == "deploying"           # proceeding like the merge sweep
        assert run.state == "deploying"
        assert run.run_log == "final logs"
        assert p.dev_pr_number == 3
        assert any(r.get("number") == 3 for r in (req.pr_urls or []))
    assert dispatched and dispatched[0][1]["kwargs"]["run_id"] == run_id
    assert any("already merged" in m for m in _messages(pid))


def test_adopt_merged_change_ignores_open_changes(org_id, quiet, monkeypatch):
    pid = _project(org_id, dev_run_state="running", gitlab_project_id=4242)
    req_id, run_id = _request_and_run(pid, "running", pr_number=3)
    monkeypatch.setattr(tasks.gitlab, "get_mr",
                        lambda glid, num: {"state": "opened"})
    with SyncSession() as db:
        p = db.get(Project, pid)
        dev_concurrency.bind_run(p, db.get(DevRun, run_id))
        assert tasks._adopt_merged_change(db, p, "main", "") is False


def test_adopt_merged_change_guards_foreign_urls(org_id, quiet, monkeypatch):
    # a chain pointer whose URL names ANOTHER repo must not resolve its number
    # against the platform project
    pid = _project(org_id, dev_run_state="running", gitlab_project_id=4242,
                   gitlab_web_url="https://gitlab.example.com/plat/proj")
    req_id, run_id = _request_and_run(
        pid, "running", pr_number=9,
        pr_url="https://github.com/acme/other/pull/9")
    monkeypatch.setattr(tasks.gitlab, "get_mr",
                        lambda glid, num: pytest.fail("foreign URL must not probe"))
    with SyncSession() as db:
        p = db.get(Project, pid)
        dev_concurrency.bind_run(p, db.get(DevRun, run_id))
        assert tasks._adopt_merged_change(db, p, "main", "") is False
