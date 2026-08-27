"""§parallel-builds: Resume scoped to ONE run. The request-thread history
console offers Resume on a failed row: in parallel mode a sibling request's
live build no longer blocks it, at limit 1 the project-level verdict stands
whole, and retry-build {run_id} chains onto exactly that row."""
import asyncio

import pytest
from sqlalchemy import delete, select, update

from app.api.serializers import (
    dev_resume_capability, dev_run_out, dev_run_resume_capability,
)
from app.core.db import SyncSession, async_session, engine
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, Request,
    StatusChange,
)
from app.services import events, project_actions
from app.workers import tasks


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: None)
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)


@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="RunResume Test Org", credit_balance=100.0)
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
                db.execute(update(Project).where(Project.id.in_(pids))
                           .values(dev_request_id=None))
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _setup(db, oid, limit=2):
    """Two requests: A's run failed and parked, B's run is live - the project
    mirror follows B (the newest started), which is exactly the shape where the
    project-level Resume says "a build is already in progress"."""
    p = Project(org_id=oid, name="P", description="d", kind="ai", status="development",
                dev_run_state="running", dev_parallel_limit=limit, gitlab_project_id=1)
    db.add(p)
    db.flush()
    a = Request(project_id=p.id, type="bug", title="A")
    b = Request(project_id=p.id, type="feature", title="B")
    db.add_all([a, b])
    db.flush()
    failed = DevRun(project_id=p.id, request_id=a.id, state="failed",
                    workspace_dir=f"devruns/{p.id}/a", branch="fix/a")
    live = DevRun(project_id=p.id, request_id=b.id, state="running",
                  workspace_dir=f"devruns/{p.id}/b", branch="feat/b")
    db.add_all([failed, live])
    db.flush()
    p.dev_request_id = b.id
    db.flush()
    return p, a, b, failed, live


def test_a_parked_sibling_resumes_beside_a_live_run(org_id):
    with SyncSession() as db:
        p, a, b, failed, live = _setup(db, org_id)
        # the project-level verdict (the overview button) stays what it was
        assert dev_resume_capability(p) == (False, "A build is already in progress")
        ctx = ({b.id}, {failed.id})
        assert dev_run_resume_capability(
            p, failed, inflight_request_ids=ctx[0], latest_failed_ids=ctx[1]) == (True, None)
        ok, why = dev_run_resume_capability(
            p, live, inflight_request_ids=ctx[0], latest_failed_ids=ctx[1])
        assert not ok and "did not fail" in why
        out = dev_run_out(failed, p, None, ctx)
        assert (out["can_resume"], out["resume_blocker"]) == (True, None)
        # no context: never guessed resumable
        assert dev_run_out(failed, p, None)["can_resume"] is False
        # its own request already building: refused with acquire_slot's copy
        assert dev_run_resume_capability(
            p, failed, inflight_request_ids={a.id, b.id}, latest_failed_ids={failed.id},
        ) == (False, "This request already has a build in flight")
        # an older failed row of the same request is not the one to continue
        older = DevRun(project_id=p.id, request_id=a.id, state="failed",
                       workspace_dir=f"devruns/{p.id}/a0")
        db.add(older)
        db.flush()
        ok, why = dev_run_resume_capability(
            p, older, inflight_request_ids={b.id}, latest_failed_ids={failed.id})
        assert not ok and why.startswith("A later build")
        # serialized (limit 1): one workspace, the project-level verdict whole
        p.dev_parallel_limit = 1
        assert dev_run_resume_capability(
            p, failed, inflight_request_ids={b.id}, latest_failed_ids={failed.id},
        ) == (False, "A build is already in progress")
        db.rollback()


def test_retry_build_by_run_id_chains_onto_that_run(org_id, quiet, monkeypatch):
    # Async-pool healing (the TestClient modules' pattern): the global engine
    # and the WS redis client may be bound to an earlier test's event loop.
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    sent = []
    monkeypatch.setattr(project_actions.celery, "send_task",
                        lambda *a, **k: sent.append((a, k)))
    with SyncSession() as db:
        p, a, b, failed, live = _setup(db, org_id)
        db.commit()
        pid, aid, fid, lid = p.id, a.id, failed.id, live.id

    async def _run():
        async with async_session() as adb:
            proj = await adb.get(Project, pid)
            inflight, latest = await project_actions.run_resume_sets(adb, proj)
            assert (inflight, latest) == ({proj.dev_request_id}, {fid})
            await project_actions.retry_build(adb, proj, is_admin=True, run_id=fid)
            for bad, status in (("nope", 404), (lid, 409)):
                try:
                    await project_actions.retry_build(adb, proj, is_admin=True, run_id=bad)
                    raise AssertionError(f"run {bad} must be refused")
                except project_actions.ActionError as exc:
                    assert exc.status == status

    asyncio.run(_run())
    with SyncSession() as db:
        new = db.execute(select(DevRun).where(DevRun.predecessor_id == fid)).scalar_one()
        assert (new.state, new.request_id, new.workspace_dir, new.branch) == (
            "queued", aid, f"devruns/{pid}/a", "fix/a")
        new_id = new.id
    assert sent and sent[-1][1]["kwargs"] == {"fix_only": True, "run_id": new_id}
