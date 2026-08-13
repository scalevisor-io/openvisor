"""§parallel-builds MR3: enforcement flips to the effective limit. Two runs
admit at limit 2 with isolated workspace dirs, mode exclusivity keeps legacy
and parallel runs off one working tree, chains inherit the parked dir/branch,
slots_full drives the chat/sweep gates, and the rollup keeps the project in
development while a sibling is live.
"""
import pytest
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.core.db import SyncSession
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, Request,
    StatusChange,
)
from app.services import dev_concurrency, events
from app.workers import tasks


@pytest.fixture
def quiet(monkeypatch):
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)
    return ws


@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="ParallelOn Test Org", credit_balance=100.0)
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


def _project(db, oid, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "idle")
    p = Project(org_id=oid, **kw)
    db.add(p)
    db.flush()
    return p


def _req(db, p, title, rtype="feature"):
    r = Request(project_id=p.id, type=rtype, title=title)
    db.add(r)
    db.flush()
    return r


def test_two_runs_admit_at_limit_two_with_isolated_dirs(org_id):
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=2)
        r1, r2, r3 = _req(db, p, "A"), _req(db, p, "B"), _req(db, p, "C")
        db.commit()
        run1 = dev_concurrency.acquire_slot(db, p, r1)
        db.commit()
        run2 = dev_concurrency.acquire_slot(db, p, r2)
        db.commit()
        assert run1.workspace_dir == f"devruns/{p.id}/{run1.id}"
        assert run2.workspace_dir == f"devruns/{p.id}/{run2.id}"
        assert run1.workspace_dir != run2.workspace_dir
        # slot 3 refused at limit 2, with today's copy
        with pytest.raises(dev_concurrency.SlotRefused) as exc:
            dev_concurrency.acquire_slot(db, p, r3)
        assert str(exc.value) == dev_concurrency.BUSY_DETAIL
        # same request can never hold two active runs
        run1.state = "failed"
        db.flush()
        with pytest.raises(dev_concurrency.SlotRefused):
            dev_concurrency.acquire_slot(db, p, r2)
        db.rollback()


def test_mode_exclusivity_legacy_vs_parallel(org_id):
    with SyncSession() as db:
        p = _project(db, org_id)  # limit 1 -> legacy mode
        r1, r2 = _req(db, p, "A"), _req(db, p, "B")
        db.commit()
        legacy = dev_concurrency.acquire_slot(db, p, r1)
        assert legacy.workspace_dir == ""
        db.commit()
        # admin raises the limit mid-run: a parallel admission must wait for
        # the legacy run that actively uses the project checkout
        p.dev_parallel_limit = 3
        db.flush()
        with pytest.raises(dev_concurrency.SlotRefused):
            dev_concurrency.acquire_slot(db, p, r2)
        # ...but once the legacy run parks on the customer's merge (its branch
        # and PR are pushed, no runner is executing), the raised limit takes
        # effect: the sibling admits with an isolated dir
        legacy.state = "awaiting_merge"
        db.flush()
        sibling = dev_concurrency.acquire_slot(db, p, r2)
        assert sibling.workspace_dir == f"devruns/{p.id}/{sibling.id}"
        db.rollback()


def test_revise_chain_converts_to_parallel_past_a_live_sibling(org_id):
    """§revise once siblings exist: continuing a superseded awaiting-merge
    legacy run must not reclaim the canonical checkout (a sibling's merge
    hard-resets it). The branch is pushed, so the revision gets an isolated
    dir and the runner entrypoint continues from origin/<branch>."""
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=3)
        r1, r2 = _req(db, p, "A"), _req(db, p, "B")
        db.commit()
        legacy = DevRun(project_id=p.id, request_id=r1.id, state="awaiting_merge",
                        workspace_dir="", branch="agent/feature-a", pr_number=7)
        db.add(legacy)
        db.add(DevRun(project_id=p.id, request_id=r2.id, state="running",
                      workspace_dir=f"devruns/{p.id}/sib"))
        db.commit()
        predecessor = dev_concurrency.release_for_revision(db, p, r1)
        assert predecessor.id == legacy.id and predecessor.state == "superseded"
        run = dev_concurrency.acquire_slot(db, p, r1, predecessor=predecessor)
        assert run.workspace_dir == f"devruns/{p.id}/{run.id}"
        assert run.branch == "agent/feature-a"  # the open PR keeps collecting
        db.rollback()


def test_failed_legacy_chain_still_waits_for_parallel_siblings(org_id):
    """The conversion is for PUSHED work only: a failed legacy run's unpushed
    branch lives in the canonical checkout, so its resume stays legacy-mode
    and must wait for every parallel sibling."""
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=3)
        r1, r2 = _req(db, p, "A"), _req(db, p, "B")
        db.commit()
        failed = DevRun(project_id=p.id, request_id=r1.id, state="failed",
                        workspace_dir="", branch="agent/feature-a")
        db.add(failed)
        db.add(DevRun(project_id=p.id, request_id=r2.id, state="running",
                      workspace_dir=f"devruns/{p.id}/sib"))
        db.commit()
        with pytest.raises(dev_concurrency.SlotRefused):
            dev_concurrency.acquire_slot(db, p, r1, predecessor=failed)
        db.rollback()


def test_resume_chains_inherit_dir_and_branch(org_id):
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=2)
        r1 = _req(db, p, "A")
        db.commit()
        run1 = dev_concurrency.acquire_slot(db, p, r1)
        run1.state = "failed"
        run1.branch = "agent/feature-a"
        db.commit()
        pid, rid = p.id, r1.id
    run_id = dev_concurrency.acquire_for_project(pid, rid)
    with SyncSession() as db:
        chained = db.get(DevRun, run_id)
        first = db.get(DevRun, run1.id)
        assert chained.predecessor_id == first.id
        assert chained.workspace_dir == first.workspace_dir
        assert chained.branch == "agent/feature-a"
        db.execute(delete(DevRun).where(DevRun.id == run_id))
        db.commit()


def test_slots_full_gate(org_id):
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=2)
        r1 = _req(db, p, "A")
        db.commit()
        assert not dev_concurrency.slots_full(db, p)
        dev_concurrency.acquire_slot(db, p, r1)
        db.flush()
        # one of two slots used: the chat/sweep gates stay open
        assert not dev_concurrency.slots_full(db, p)
        r2 = _req(db, p, "B")
        dev_concurrency.acquire_slot(db, p, r2)
        db.flush()
        assert dev_concurrency.slots_full(db, p)
        db.rollback()
        # legacy limit 1: the scalar in-flight check still counts
        p2 = _project(db, org_id, dev_run_state="running")
        assert dev_concurrency.slots_full(db, p2)
        db.rollback()


def test_rollup_keeps_project_in_development_while_sibling_lives(org_id, quiet):
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=2)
        r1, r2 = _req(db, p, "A"), _req(db, p, "B")
        db.commit()
        run1 = dev_concurrency.acquire_slot(db, p, r1)
        db.commit()
        run2 = dev_concurrency.acquire_slot(db, p, r2)
        run2.state = "running"
        db.commit()
        # run1 parks while run2 is live: the project must stay development
        dev_concurrency.bind_run(p, run1)
        tasks._safe_transition(db, p, "awaiting_customer", "park with sibling")
        assert p.status == "development"
        # last active run parks: the transition goes through
        run2.state = "failed"
        db.flush()
        tasks._safe_transition(db, p, "awaiting_customer", "park alone")
        assert p.status == "awaiting_customer"
        db.rollback()


def test_recompute_mirror_follows_newest_active_sibling(org_id):
    from app.models import utcnow
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=2, dev_run_state="running")
        r1, r2 = _req(db, p, "A"), _req(db, p, "B")
        db.commit()
        run1 = dev_concurrency.acquire_slot(db, p, r1)
        db.commit()
        run2 = dev_concurrency.acquire_slot(db, p, r2)
        run2.state = "running"
        run2.started_at = utcnow()
        run2.branch = "agent/feature-b"
        run2.pr_number = 9
        db.flush()
        run1.state = "done"
        db.flush()
        tasks._recompute_mirror(db, p)
        assert p.dev_run_state == "running"
        assert p.dev_request_id == r2.id
        assert p.dev_branch == "agent/feature-b"
        assert p.dev_pr_number == 9
        db.rollback()


def test_primary_run_resolution_tracks_the_mirror(org_id):
    """primary_run = what the Project.dev_* mirror shows: the most recently
    STARTED active row, sticky to the newest-started row once all finished,
    None before anything ever started."""
    from datetime import timedelta
    from app.models import utcnow
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=3)
        db.flush()
        assert dev_concurrency.primary_run(db, p) is None
        t0 = utcnow()
        legacy = DevRun(project_id=p.id, state="awaiting_merge", workspace_dir="",
                        started_at=t0 - timedelta(minutes=10))
        parallel = DevRun(project_id=p.id, state="running",
                          workspace_dir=f"devruns/{p.id}/r1", started_at=t0)
        queued = DevRun(project_id=p.id, state="queued", workspace_dir="")
        db.add_all([legacy, parallel, queued])
        db.flush()
        assert dev_concurrency.primary_run(db, p).id == parallel.id
        # all terminal -> sticky on the newest-started row
        legacy.state, parallel.state = "done", "done"
        db.flush()
        assert dev_concurrency.primary_run(db, p).id == parallel.id
        db.rollback()


def test_feed_path_follows_the_primary_run(org_id, tmp_path, monkeypatch):
    """The live console's no-run_id read must serve the primary run's OWN feed
    when it is parallel-mode - not replay the stale legacy project file
    (prod regression: the request console showed the previous run's story)."""
    from app.models import utcnow
    from app.services import devfeed
    monkeypatch.setattr(settings, "workspaces_dir", str(tmp_path))
    with SyncSession() as db:
        ws = tmp_path / "proj"
        (ws / ".openvisor").mkdir(parents=True)
        (ws / ".openvisor" / "events.jsonl").write_text('{"title": "OLD RUN"}\n')
        p = _project(db, org_id, dev_parallel_limit=3, workspace_path=str(ws))
        run = DevRun(project_id=p.id, state="running",
                     workspace_dir=f"devruns/{p.id}/r1", started_at=utcnow())
        db.add(run)
        db.flush()
        # unbound (legacy default): the stale project file
        assert devfeed.feed_path(p) == ws / ".openvisor" / "events.jsonl"
        # bound to the resolved primary: the run's own workspace feed
        dev_concurrency.bind_run(p, dev_concurrency.primary_run(db, p))
        assert devfeed.feed_path(p) == (tmp_path / f"devruns/{p.id}/r1"
                                        / ".openvisor" / "events.jsonl")
        db.rollback()


def test_primary_for_project_twin_returns_the_detached_row(org_id):
    from app.models import utcnow
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=2)
        run = DevRun(project_id=p.id, state="running",
                     workspace_dir=f"devruns/{p.id}/r1", started_at=utcnow())
        db.add(run)
        db.commit()
        pid, rid = p.id, run.id
    row = dev_concurrency.primary_for_project(pid)
    assert row is not None and row.id == rid
    assert row.workspace_dir == f"devruns/{pid}/r1"  # attribute usable detached
