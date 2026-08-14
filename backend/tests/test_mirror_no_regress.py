"""§parallel-builds: a finishing run must not regress the shared display mirror.

`bound_run` is a per-worker in-memory attribute, so each run updates its own
DevRun row correctly. But `Project.dev_run_state` is ONE scalar for the whole
project and `_save_run` wrote it unconditionally - so when two runs overlapped
(the limit here is 3), whichever finished first announced its state to the
panel over a sibling that was still building.

Seen in production: a pricing run closed while the Trust-page run had ten
minutes left, the Development panel said "Development complete - the build
finished successfully", and the customer reasonably tried to retry a build that
had never stopped. The row ledger was right the whole time; only the mirror
lied.
"""
import pytest

from app.core.db import SyncSession
from app.models import DevRun, Organization, Project, Request, utcnow
from app.services import dev_concurrency
from app.workers import tasks


def _project(db):
    org = Organization(name="Mirror Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai",
                status="development")
    db.add(p)
    db.flush()
    return p


def _run(db, project, state, minutes_ago, request_id=None, branch=None):
    """request_id is a real FK, so each named run gets a real Request row."""
    from datetime import timedelta
    if request_id is not None:
        req = Request(project_id=project.id, type="feature", handling="ai",
                      status="in_progress", title=request_id)
        db.add(req)
        db.flush()
        request_id = req.id
    run = DevRun(project_id=project.id, state=state, request_id=request_id,
                 branch=branch, started_at=utcnow() - timedelta(minutes=minutes_ago))
    db.add(run)
    db.flush()
    return run


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """The mirror is the subject; the feed/WS side effects are not."""
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(tasks.events, "publish_sync", lambda *a, **k: None)


def test_a_finishing_run_leaves_the_mirror_on_a_live_sibling():
    """The prod case: the older run finishes, the newer one is still building."""
    with SyncSession() as db:
        try:
            project = _project(db)
            older = _run(db, project, "running", minutes_ago=12,
                         request_id="req-older", branch="agent/pricing")
            newer = _run(db, project, "running", minutes_ago=8,
                         request_id="req-newer", branch="agent/trust-page")
            project.dev_run_state = "running"
            project.dev_request_id = newer.request_id
            project._dev_run = older          # this worker owns the older run

            tasks._save_run(project, "done", logs="finished")
            db.flush()

            assert older.state == "done"                  # its own row closes
            assert newer.state == "running"               # the sibling is untouched
            assert project.dev_run_state == "running"     # panel follows the live run
            assert project.dev_request_id == newer.request_id
            assert project.dev_branch == "agent/trust-page"
        finally:
            db.rollback()


def test_the_last_run_finishing_still_settles_the_mirror():
    """With no sibling left, the terminal state must stand - otherwise a project
    would never look finished."""
    with SyncSession() as db:
        try:
            project = _project(db)
            only = _run(db, project, "running", minutes_ago=5, request_id="req-only")
            project.dev_run_state = "running"
            project._dev_run = only

            tasks._save_run(project, "done", logs="finished")
            db.flush()

            assert only.state == "done"
            assert project.dev_run_state == "done"
        finally:
            db.rollback()


def test_a_failure_does_not_regress_the_mirror_either():
    """Same rule for the unhappy path: one run failing must not tell the panel
    the project is failed while another builds."""
    with SyncSession() as db:
        try:
            project = _project(db)
            older = _run(db, project, "running", minutes_ago=12, request_id="req-older")
            newer = _run(db, project, "running", minutes_ago=3, request_id="req-newer")
            project.dev_run_state = "running"
            project._dev_run = older

            tasks._save_run(project, "failed", logs="boom", error="it broke")
            db.flush()

            assert older.state == "failed"
            assert project.dev_run_state == "running"
            assert newer.state == "running"
        finally:
            db.rollback()


def test_a_run_going_active_still_takes_the_mirror():
    """Only TERMINAL states defer to a sibling: a run entering an active state is
    the newest thing happening and should own the panel."""
    with SyncSession() as db:
        try:
            project = _project(db)
            older = _run(db, project, "running", minutes_ago=12, request_id="req-older")
            mine = _run(db, project, "queued", minutes_ago=1, request_id="req-mine")
            project.dev_run_state = "running"
            project._dev_run = mine

            tasks._save_run(project, "running", logs="starting")
            db.flush()

            assert project.dev_run_state == "running"
            assert mine.state == "running"
            assert older.state == "running"
        finally:
            db.rollback()


def test_the_active_states_the_rule_keys_on_are_the_shared_ones():
    """The guard reads dev_concurrency.ACTIVE_ROW_STATES rather than a private
    list, so admission and the mirror can never disagree about 'still building'."""
    assert "running" in dev_concurrency.ACTIVE_ROW_STATES
    assert "awaiting_merge" in dev_concurrency.ACTIVE_ROW_STATES
    assert "done" not in dev_concurrency.ACTIVE_ROW_STATES
    assert "failed" not in dev_concurrency.ACTIVE_ROW_STATES
