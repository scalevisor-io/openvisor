"""§parallel-builds MR1 (docs/PARALLEL_BUILDS.md): the DevRun ledger and the
entitlement chokepoint, dark. The gate at limit 1 must be provably identical to
the pre-ledger behavior: it refuses in exactly today's three in-flight scalar
states with today's exact copy, and everything else acquires. The ledger rows
shadow the run via _save_run without anything reading them for behavior.
"""
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

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
        org = Organization(name="DevConc Test Org", credit_balance=100.0)
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


def test_effective_limit_resolution(org_id, monkeypatch):
    with SyncSession() as db:
        p = _project(db, org_id)
        # null override -> instance default (1)
        assert dev_concurrency.effective_parallel_limit(db, p) == 1
        # override clamped by the instance hard ceiling
        p.dev_parallel_limit = 99
        assert dev_concurrency.effective_parallel_limit(db, p) == settings.dev_parallel_runs_max
        p.dev_parallel_limit = 2
        assert dev_concurrency.effective_parallel_limit(db, p) == 2
        # the licensing hook caps everything when it answers
        monkeypatch.setattr(dev_concurrency, "_entitlement_limit", lambda db, org: 1)
        assert dev_concurrency.effective_parallel_limit(db, p) == 1
        # floor at 1: a broken override can never disable builds entirely
        monkeypatch.setattr(dev_concurrency, "_entitlement_limit", lambda db, org: 0)
        assert dev_concurrency.effective_parallel_limit(db, p) == 1
        db.rollback()


def test_gate_identity_with_todays_states_and_copy(org_id):
    # The provable-identity contract: refusal in EXACTLY today's three in-flight
    # scalar states, with today's exact handle_request copy.
    assert dev_concurrency.BUSY_DETAIL == (
        "A build is already in progress for this project - "
        "re-submit this request once it completes.")
    for state in ("running", "awaiting_merge", "deploying"):
        with SyncSession() as db:
            p = _project(db, org_id, dev_run_state=state)
            db.commit()
            with pytest.raises(dev_concurrency.SlotRefused) as exc:
                dev_concurrency.acquire_slot(db, p)
            assert str(exc.value) == dev_concurrency.BUSY_DETAIL
            db.rollback()
    for state in ("idle", "failed", "done"):
        with SyncSession() as db:
            p = _project(db, org_id, dev_run_state=state)
            db.commit()
            run = dev_concurrency.acquire_slot(db, p)
            assert run.state == "queued" and run.project_id == p.id
            db.rollback()


def test_request_resolution_and_row_gate(org_id):
    with SyncSession() as db:
        p = _project(db, org_id)
        mvp = Request(project_id=p.id, type="mvp", title="Initial build")
        scoped = Request(project_id=p.id, type="feature", title="Exports")
        db.add_all([mvp, scoped])
        db.flush()
        db.commit()
        # no scoped run in flight -> Request #0
        run = dev_concurrency.acquire_slot(db, p)
        assert run.request_id == mvp.id
        db.commit()
        # an active ledger row holds the slot even while scalars read idle
        with pytest.raises(dev_concurrency.SlotRefused):
            dev_concurrency.acquire_slot(db, p)
        db.rollback()
        # scoped pointer wins the default resolution
        with SyncSession() as db2:
            db2.execute(update(DevRun).values(state="failed"))
            p2 = db2.get(Project, p.id)
            p2.dev_request_id = scoped.id
            run2 = dev_concurrency.acquire_slot(db2, p2)
            assert run2.request_id == scoped.id
            db2.rollback()


def test_wallet_floor_admission(org_id, monkeypatch):
    monkeypatch.setattr(settings, "dev_run_credit_floor", 50.0)
    with SyncSession() as db:
        p = _project(db, org_id)
        db.commit()
        run = dev_concurrency.acquire_slot(db, p)  # balance 100 >= 1*50
        assert run is not None
        db.rollback()
    with SyncSession() as db:
        org = db.get(Organization, org_id)
        org.credit_balance = 10.0
        p = _project(db, org_id)
        db.commit()
        with pytest.raises(dev_concurrency.SlotRefused) as exc:
            dev_concurrency.acquire_slot(db, p)
        assert "credit balance" in str(exc.value)
        org = db.get(Organization, org_id)
        org.credit_balance = 100.0
        db.commit()


def test_adopt_or_create_bridge(org_id):
    with SyncSession() as db:
        p = _project(db, org_id)
        mvp = Request(project_id=p.id, type="mvp", title="Initial build")
        db.add(mvp)
        db.flush()
        db.commit()
        acquired = dev_concurrency.acquire_slot(db, p)
        db.commit()
        # by id
        assert dev_concurrency.adopt_or_create(db, p, acquired.id).id == acquired.id
        # by active-row adoption when the id was not threaded through
        assert dev_concurrency.adopt_or_create(db, p, None).id == acquired.id
        # bridge-create when nothing is active (message queued across a deploy)
        acquired.state = "done"
        db.flush()
        bridged = dev_concurrency.adopt_or_create(db, p, None)
        assert bridged.id != acquired.id and bridged.request_id == mvp.id
        db.rollback()


def test_save_run_shadow_mirror(org_id, quiet):
    with SyncSession() as db:
        p = _project(db, org_id, dev_branch="agent/mvp-x", dev_pr_number=4,
                     dev_pr_url="https://g/x/pull/4")
        db.commit()
        run = dev_concurrency.acquire_slot(db, p)
        db.commit()
        tasks._save_run(p, "running")
        assert run.state == "running"
        tasks._save_run(p, "failed", logs="tail", error="boom")
        db.flush()
        assert (run.state, run.run_error, run.run_log) == ("failed", "boom", "tail")
        # branch back-fills from the scalar only while the row has none; the PR
        # pointer is row-authoritative and flows through _set_run_pr (§MR3)
        assert run.branch == "agent/mvp-x"
        dev_concurrency.bind_run(p, run)
        tasks._set_run_pr(p)
        assert run.pr_number == 4
        # terminal rows are never resurrected: no active row -> mirror no-ops
        tasks._save_run(p, "running")
        assert run.state == "failed"
        db.rollback()


def test_partial_index_one_active_run_per_request(org_id):
    with SyncSession() as db:
        p = _project(db, org_id)
        req = Request(project_id=p.id, type="feature", title="X")
        db.add(req)
        db.flush()
        db.add(DevRun(project_id=p.id, request_id=req.id, state="running"))
        db.flush()
        db.add(DevRun(project_id=p.id, request_id=req.id, state="queued"))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


def test_run_development_wrapper_forwards_run_id(org_id, quiet, monkeypatch):
    """The agent-eval wrapper (Phase 0) must thread run_id into the impl: MR1
    made adopt_or_create consume it, and dropping it NameError-crashed every
    dispatched run in prod (2026-08-10) while the ledger row sat queued forever."""
    seen = {}
    monkeypatch.setattr(tasks, "_run_development_impl",
                        lambda pid, fix_only=False, run_id=None:
                        seen.update(pid=pid, fix_only=fix_only, run_id=run_id))
    from app.services.agent_eval import collect
    monkeypatch.setattr(collect, "capture_run_record", lambda *a, **k: None)
    with SyncSession() as db:
        pid = _project(db, org_id).id
        db.commit()
    tasks.run_development(pid, fix_only=True, run_id="run-forwarded")
    assert seen == {"pid": pid, "fix_only": True, "run_id": "run-forwarded"}


def test_slots_full_respects_workspace_mode_exclusivity(org_id):
    """A limit raised mid-run frees nothing while a legacy-mode row actively
    uses the project checkout (prod regression: the sweep re-dispatched into
    acquire_slot's refusal every minute) - but a legacy row PARKED in
    awaiting_merge no longer vetoes parallel admission (prod regression: the
    raised limit stayed dead-lettered for as long as the customer took to
    merge). Gate and admission must agree."""
    with SyncSession() as db:
        p = _project(db, org_id, dev_parallel_limit=3)
        legacy = DevRun(project_id=p.id, state="running", workspace_dir="")
        db.add(legacy)
        db.flush()
        assert dev_concurrency.slots_full(db, p) is True  # live legacy run blocks
        # parked on a human merge: its work is pushed, the checkout is
        # quiescent - the raised limit takes effect
        legacy.state = "awaiting_merge"
        db.flush()
        assert dev_concurrency.slots_full(db, p) is False
        # ...and a parallel-mode sibling does NOT block further parallel slots
        db.query(DevRun).delete()
        db.add(DevRun(project_id=p.id, state="running",
                      workspace_dir=f"devruns/{p.id}/r1"))
        db.flush()
        assert dev_concurrency.slots_full(db, p) is False
        # the reverse stays strict: a legacy admission (limit back at 1) waits
        # for every active parallel row, parked or not - a sibling's merge
        # hard-resets the checkout a legacy run would work in
        db.query(DevRun).delete()
        db.add(DevRun(project_id=p.id, state="awaiting_merge",
                      workspace_dir=f"devruns/{p.id}/r2"))
        p.dev_parallel_limit = None
        db.flush()
        assert dev_concurrency.slots_full(db, p) is True
        db.rollback()
