"""§run chains Start fresh: discard a failed chain instead of resuming it.

Resume chains onto the failed run - same workspace, same branch - which is right
until the chain went down a bad path: then every resume keeps building on the
poisoned tree, and there was NO way to start over short of deleting the request.

Fresh has to defeat two continuation mechanisms, both pinned here:
- the workspace: a fresh run gets its own isolated dir in EVERY mode, because at
  limit 1 a chainless run would otherwise reuse the canonical checkout with the
  discarded (possibly uncommitted) work still sitting in it;
- the branch: the entrypoint continues origin/<branch> AND local unpushed
  commits, so a fresh run that re-derives the same name would silently resurrect
  the abandoned work - the request's prior runs therefore reserve their names
  and the fresh run gets a suffixed one.
"""
import pytest

from app.core.db import SyncSession
from app.models import DevRun, Organization, Project, Request, utcnow
from app.services import dev_concurrency
from app.workers import tasks


def _project(db, **kw):
    org = Organization(name="Fresh Org", credit_balance=50.0)
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai",
                status="development", workspace_path="/workspaces/legacy", **kw)
    db.add(p)
    db.flush()
    return p


def _request(db, project, title="Fix the thing"):
    req = Request(project_id=project.id, type="feature", handling="ai",
                  status="in_progress", title=title)
    db.add(req)
    db.flush()
    project.dev_request_id = req.id
    db.flush()
    return req


def _failed_run(db, project, req, branch="agent/fix-the-thing",
                workspace_dir=""):
    run = DevRun(project_id=project.id, request_id=req.id, state="failed",
                 branch=branch, workspace_dir=workspace_dir, started_at=utcnow())
    db.add(run)
    db.flush()
    return run


# ------------------------------------------------------------------ acquisition

def test_fresh_supersedes_the_chain_and_starts_unchained():
    with SyncSession() as db:
        try:
            project = _project(db)
            org_id = project.org_id
            req = _request(db, project)
            old = _failed_run(db, project, req,
                              workspace_dir=f"devruns/{project.id}/old")
            db.commit()
            run_id = dev_concurrency.acquire_for_project(project.id, fresh=True)
            run = db.get(DevRun, run_id)
            db.refresh(old)
            assert old.state == "superseded"          # the chain is closed for good
            assert run.predecessor_id is None
            assert run.branch is None                 # naming re-runs
            assert run.workspace_dir not in ("", old.workspace_dir)
        finally:
            with SyncSession() as db2:
                db2.query(Project).filter(Project.id == project.id).update(
                    {Project.dev_request_id: None}, synchronize_session=False)
                db2.query(DevRun).filter(DevRun.project_id == project.id).delete()
                db2.query(Request).filter(Request.project_id == project.id).delete()
                db2.query(Project).filter(Project.id == project.id).delete()
                db2.query(Organization).filter(Organization.id == org_id).delete()
                db2.commit()


def test_fresh_gets_an_isolated_dir_even_at_limit_one(monkeypatch):
    """At limit 1 a chainless run would otherwise land in the canonical checkout
    - the very tree the customer is trying to walk away from."""
    with SyncSession() as db:
        try:
            monkeypatch.setattr(dev_concurrency, "effective_parallel_limit",
                                lambda db, p: 1)
            project = _project(db)
            req = _request(db, project)
            _failed_run(db, project, req)             # legacy chain, no workspace_dir
            run = dev_concurrency.acquire_slot(db, project, req,
                                               predecessor=None, fresh=True)
            assert run.workspace_dir.startswith(f"devruns/{project.id}/")
        finally:
            db.rollback()


def test_plain_resume_still_chains():
    """The default path is untouched: same workspace, same branch, predecessor."""
    with SyncSession() as db:
        try:
            project = _project(db)
            req = _request(db, project)
            old = _failed_run(db, project, req,
                              workspace_dir=f"devruns/{project.id}/old")
            run = dev_concurrency.acquire_slot(
                db, project, req,
                predecessor=dev_concurrency.latest_failed_run(db, project, req))
            assert run.predecessor_id == old.id
            assert run.workspace_dir == old.workspace_dir
            assert run.branch == old.branch
        finally:
            db.rollback()


# ------------------------------------------------------------------ the branch

def test_a_fresh_run_never_rederives_the_abandoned_branch_name(monkeypatch):
    """The entrypoint continues origin/<branch> and even local unpushed commits;
    a same-name fresh run would resurrect the discarded work."""
    with SyncSession() as db:
        try:
            monkeypatch.setattr(tasks.pipeline, "generate_branch_name",
                                lambda *a, **k: "agent/fix-the-thing")
            project = _project(db)
            req = _request(db, project)
            _failed_run(db, project, req, branch="agent/fix-the-thing")
            fresh = DevRun(project_id=project.id, request_id=req.id,
                           state="queued", branch=None,
                           workspace_dir=f"devruns/{project.id}/fresh")
            db.add(fresh)
            db.flush()
            project._dev_run = fresh
            tasks._ensure_dev_branch(db, project)
            assert fresh.branch is not None
            assert fresh.branch != "agent/fix-the-thing"
            assert fresh.branch.startswith("agent/fix-the-thing-")
        finally:
            db.rollback()


def test_the_first_run_of_a_request_keeps_the_clean_name(monkeypatch):
    with SyncSession() as db:
        try:
            monkeypatch.setattr(tasks.pipeline, "generate_branch_name",
                                lambda *a, **k: "agent/fix-the-thing")
            project = _project(db)
            req = _request(db, project)
            first = DevRun(project_id=project.id, request_id=req.id,
                           state="queued", branch=None,
                           workspace_dir=f"devruns/{project.id}/first")
            db.add(first)
            db.flush()
            project._dev_run = first
            tasks._ensure_dev_branch(db, project)
            assert first.branch == "agent/fix-the-thing"
        finally:
            db.rollback()
