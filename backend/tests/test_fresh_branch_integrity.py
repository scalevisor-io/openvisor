"""§run chains: the stale-branch backfill that defeated Start fresh in prod.

Start fresh gives the run row branch=None so naming re-derives (with the
request's prior names reserved). But _save_run's shadow-ledger mirror ran
FIRST - at the state flip to running - and its backfill stamped the stale
Project.dev_branch onto any branch-less row. _ensure_dev_branch then saw a
branch and early-returned: the "fresh" run landed on the abandoned branch, the
entrypoint's origin/<branch> continuation resurrected the discarded commit, and
the session concluded "work already done" (prod run f8e688df, third failure of
one routine request). The backfill now applies to CHAINED rows only.
"""
import pytest

from app.core.db import SyncSession
from app.models import DevRun, Organization, Project, Request, utcnow
from app.workers import tasks


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(tasks.events, "publish_sync", lambda *a, **k: None)


def _setup(db):
    org = Organization(name="Backfill Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    project = Project(org_id=org.id, name="P", description="d", kind="ai",
                      status="development", dev_branch="agent/old-abandoned")
    db.add(project)
    db.flush()
    req = Request(project_id=project.id, type="feature", handling="ai",
                  status="in_progress", title="T")
    db.add(req)
    db.flush()
    return project, req


def test_an_unchained_row_is_never_stamped_with_the_stale_scalar():
    """The prod bug verbatim: fresh row, stale Project.dev_branch, _save_run
    fires before naming - the row must stay branch-less."""
    with SyncSession() as db:
        try:
            project, req = _setup(db)
            fresh = DevRun(project_id=project.id, request_id=req.id,
                           state="queued", branch=None, predecessor_id=None,
                           workspace_dir=f"devruns/{project.id}/fresh",
                           started_at=utcnow())
            db.add(fresh)
            db.flush()
            project._dev_run = fresh

            tasks._save_run(project, "running", logs="starting")
            db.flush()
            assert fresh.branch is None          # naming still gets to run
        finally:
            db.rollback()


def test_a_chained_row_still_gets_the_legacy_backfill():
    with SyncSession() as db:
        try:
            project, req = _setup(db)
            pred = DevRun(project_id=project.id, request_id=req.id, state="failed",
                          branch="agent/old-abandoned", started_at=utcnow())
            db.add(pred)
            db.flush()
            chained = DevRun(project_id=project.id, request_id=req.id,
                             state="queued", branch=None,
                             predecessor_id=pred.id, workspace_dir="",
                             started_at=utcnow())
            db.add(chained)
            db.flush()
            project._dev_run = chained

            tasks._save_run(project, "running", logs="starting")
            db.flush()
            assert chained.branch == "agent/old-abandoned"
        finally:
            db.rollback()
