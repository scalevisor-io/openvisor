"""Stale dev-run reaper (§14.x) DB-backed tests, test_programs_db.py style.

run_development is synchronous and Celery is not acks_late, so a worker that dies
mid-run strands the project in an in-flight dev sub-state forever (Resume
blocked, orphaned dev job burning unmetered tokens). dev_run_reaper recovers such
runs. These tests cover: an orphan past timeout+grace is parked as a normal
failed+resumable run (usage recovered, WS emitted, status → awaiting_customer); a
fresh run and awaiting_merge are left alone; the task never raises (Beat keeps
ticking); billing runs exactly once on a present usage.json and no-ops when
absent; and Resume lights up on a reaped project.
"""
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.api.serializers import dev_resume_capability
from app.core.config import settings
from app.core.db import SyncSession
from app.models import (
    DevRun,
    CreditTransaction, Message, Organization, Project, StatusChange, utcnow,
)
from app.services import events
from app.services.pricing import cost_credits
from app.workers import tasks
from app.workers.celery_app import celery


def _stale_dt():
    """A start time comfortably past the reap threshold."""
    return utcnow() - timedelta(
        minutes=settings.dev_run_timeout_minutes + settings.dev_run_reap_grace_minutes + 5)


@pytest.fixture
def quiet(monkeypatch):
    """Detach the reaper from redis/broker: capture WS events, swallow the email
    dispatch, so _post_message / transition_sync run without external services."""
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(celery, "send_task", lambda *a, **k: None)
    return ws


# ---- in-session helpers (flush + rollback, hermetic) ----

def _org(db, balance=100.0):
    org = Organization(name="Reaper Test Org", credit_balance=balance)
    db.add(org)
    db.flush()
    return org


def _project(db, org, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("gitlab_project_id", 1)
    kw.setdefault("dev_run_state", "running")
    kw.setdefault("dev_run_started_at", _stale_dt())
    p = Project(org_id=org.id, **kw)
    db.add(p)
    db.flush()
    return p


# ---- committed-row helpers (dev_run_reaper opens its own session) ----

@pytest.fixture
def reaper_org():
    """Committed throwaway org; removes every row scoped to it afterwards so the
    shared dev DB is left untouched (the task runs in its own session, so its
    target rows must be committed to be visible)."""
    with SyncSession() as db:
        org = Organization(name="Reaper Test Org", credit_balance=100.0)
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
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _commit_project(oid, **kw):
    with SyncSession() as db:
        org = db.get(Organization, oid)
        p = _project(db, org, **kw)
        db.commit()
        return p.id


# ---- detection / threshold (full task, own session) ----

def test_reaper_parks_run_stuck_past_threshold(reaper_org, quiet):
    pid = _commit_project(reaper_org, dev_run_state="running", status="development")
    tasks.dev_run_reaper()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "failed"
        assert "resumable" in (p.dev_run_error or "")
        assert p.status == "awaiting_customer"  # normal failure path
        enabled, blocker = dev_resume_capability(p)  # Resume lights up again
        assert enabled and blocker is None
    # the failure surfaced to the customer over the WS bus, exactly like a live
    # failure: a status update and at least one chat message
    assert any(ev.get("type") == "status" and ev.get("status") == "awaiting_customer"
               for _pid, ev in quiet)
    assert any(ev.get("type") == "message" for _pid, ev in quiet)


def test_reaper_parks_deploying_orphan(reaper_org, quiet):
    pid = _commit_project(reaper_org, dev_run_state="deploying", status="development")
    tasks.dev_run_reaper()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "failed"
        assert p.status == "awaiting_customer"


def test_reaper_leaves_fresh_run_alone(reaper_org, quiet):
    fresh = utcnow() - timedelta(minutes=2)
    pid = _commit_project(reaper_org, dev_run_state="running", dev_run_started_at=fresh)
    tasks.dev_run_reaper()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "running"  # a live build is never reaped
        assert p.status == "development"
    assert quiet == []  # nothing emitted, nothing changed


def test_reaper_leaves_run_just_inside_grace_alone(reaper_org, quiet):
    # one minute short of the threshold: still a plausibly-live build
    edge = utcnow() - timedelta(
        minutes=settings.dev_run_timeout_minutes + settings.dev_run_reap_grace_minutes - 1)
    pid = _commit_project(reaper_org, dev_run_state="running", dev_run_started_at=edge)
    tasks.dev_run_reaper()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "running"
    assert quiet == []


def test_reaper_never_touches_awaiting_merge(reaper_org, quiet):
    # awaiting_merge waits for the customer to merge the PR - dev_pr_sweep owns its
    # liveness - so it is never an orphan, however old the (original run) clock is
    ancient = utcnow() - timedelta(days=3)
    pid = _commit_project(reaper_org, dev_run_state="awaiting_merge",
                          dev_run_started_at=ancient, status="awaiting_customer",
                          dev_pr_number=7)
    tasks.dev_run_reaper()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "awaiting_merge"
    assert quiet == []


# ---- platform-GitLab recovery: a run interrupted after the MR was opened ----
# The runner opens the agent/mvp MR via a push option DURING the platform build,
# so a worker killed before the inline auto-merge leaves a LIVE MR. The reaper
# must hand it to dev_pr_sweep (awaiting_merge), NOT fail it with a misleading
# "nothing was published" - otherwise the customer merges and nothing deploys.

_PLATFORM = dict(gitlab_ssh_url="ssh://git@gl/acme/x.git", gitlab_project_id=42)


def test_reaper_recovers_platform_mr_as_awaiting_merge(reaper_org, monkeypatch, quiet):
    monkeypatch.setattr(tasks.gitlab, "find_open_mr",
                        lambda pid, br: {"iid": 5, "web_url": "https://gl/mr/5"})
    monkeypatch.setattr(tasks, "_bill_dev_run", lambda db, p: None)
    pid = _commit_project(reaper_org, dev_run_state="running", status="development",
                          **_PLATFORM)
    tasks.dev_run_reaper()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"   # recovered, NOT failed
        assert p.dev_pr_number == 5                  # sweep now has the MR to watch
        assert p.status == "awaiting_customer"
    assert any("merge request !5" in ev["message"]["body"]
               for _pid, ev in quiet if ev.get("type") == "message")


def test_reaper_platform_no_open_mr_falls_through_to_failed(reaper_org, monkeypatch, quiet):
    # nothing was actually published (no MR) → the ordinary failed+resumable park
    monkeypatch.setattr(tasks.gitlab, "find_open_mr", lambda pid, br: None)
    pid = _commit_project(reaper_org, dev_run_state="running", status="development",
                          **_PLATFORM)
    tasks.dev_run_reaper()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "failed"
        assert p.status == "awaiting_customer"


def test_reaper_platform_mr_lookup_error_falls_through(reaper_org, monkeypatch, quiet):
    # platform GitLab unreachable → fail safe to the ordinary reap, never crash
    def boom(pid, br):
        raise RuntimeError("gitlab down")
    monkeypatch.setattr(tasks.gitlab, "find_open_mr", boom)
    pid = _commit_project(reaper_org, dev_run_state="running", status="development",
                          **_PLATFORM)
    tasks.dev_run_reaper()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "failed"


# ---- dev_pr_sweep now merges + deploys a recovered platform MR ----

def test_sweep_platform_mr_merged_deploys(reaper_org, monkeypatch, quiet):
    monkeypatch.setattr(tasks.gitlab, "get_mr", lambda gid, iid: {"state": "merged"})
    deployed = []
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: deployed.append(a))
    pid = _commit_project(reaper_org, dev_run_state="awaiting_merge",
                          status="awaiting_customer", dev_pr_number=5, **_PLATFORM)
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
    assert deployed


def test_sweep_platform_mr_open_arms_then_merges(reaper_org, monkeypatch, quiet):
    monkeypatch.setattr(tasks.gitlab, "get_mr", lambda gid, iid: {"state": "opened"})
    armed = []
    monkeypatch.setattr(tasks.gitlab, "auto_merge",
                        lambda gid, iid, timeout_s=45, squash=True: armed.append(iid) or (True, "merged"))
    deployed = []
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: deployed.append(a))
    pid = _commit_project(reaper_org, dev_run_state="awaiting_merge",
                          status="awaiting_customer", dev_pr_number=5, **_PLATFORM)
    tasks.dev_pr_sweep()
    assert armed == [5]                              # re-armed auto-merge-on-green
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
    assert deployed


def test_sweep_platform_mr_open_ci_pending_waits(reaper_org, monkeypatch, quiet):
    # CI still running: auto_merge armed merge_when_pipeline_succeeds server-side
    # and returned ci_timeout - stay awaiting_merge, a later tick sees it merged.
    monkeypatch.setattr(tasks.gitlab, "get_mr", lambda gid, iid: {"state": "opened"})
    monkeypatch.setattr(tasks.gitlab, "auto_merge",
                        lambda gid, iid, timeout_s=45, squash=True: (False, "ci_timeout"))
    deployed = []
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: deployed.append(a))
    pid = _commit_project(reaper_org, dev_run_state="awaiting_merge",
                          status="awaiting_customer", dev_pr_number=5, **_PLATFORM)
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "awaiting_merge"  # still waiting
    assert deployed == []


def test_sweep_platform_mr_closed_fails(reaper_org, monkeypatch, quiet):
    monkeypatch.setattr(tasks.gitlab, "get_mr", lambda gid, iid: {"state": "closed"})
    pid = _commit_project(reaper_org, dev_run_state="awaiting_merge",
                          status="awaiting_customer", dev_pr_number=5, **_PLATFORM)
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "failed"


def test_reaper_skips_run_without_start_clock(reaper_org, quiet):
    # a NULL clock can't be judged stale - never reap it (no false positive)
    pid = _commit_project(reaper_org, dev_run_state="running", dev_run_started_at=None)
    tasks.dev_run_reaper()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "running"
    assert quiet == []


def test_reaper_never_crashes_on_row_error(reaper_org, monkeypatch, quiet):
    # a single bad row must not take down the Beat task
    pid = _commit_project(reaper_org, dev_run_state="running")

    def boom(db, project):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tasks, "_reap_dev_run", boom)
    assert tasks.dev_run_reaper() is None  # swallowed
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "running"  # left untouched


# ---- billing reconciliation (in-session, rollback) ----

def test_reap_bills_present_usage_exactly_once(tmp_path, quiet):
    internal = tmp_path / ".openvisor"
    internal.mkdir()
    (internal / "usage.json").write_text(
        '{"model": "mistral-large-latest", "input_tokens": 1000, "output_tokens": 500}')
    expected = cost_credits("mistral-large-latest", 1000, 500)
    with SyncSession() as db:
        org = _org(db)
        p = _project(db, org, workspace_path=str(tmp_path))
        tasks._reap_dev_run(db, p)
        assert p.dev_run_state == "failed"
        assert "resumable" in p.dev_run_error and "unmetered" not in p.dev_run_error
        assert org.credit_balance == pytest.approx(100.0 - expected)
        txns = db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == org.id)).scalars().all()
        assert len(txns) == 1 and txns[0].kind == "consumption"
        assert not (internal / "usage.json").exists()  # unlinked → never billed twice
        db.rollback()


def test_reap_without_usage_report_notes_unmetered(quiet):
    with SyncSession() as db:
        org = _org(db)
        p = _project(db, org, workspace_path="/nonexistent/does-not-exist")
        tasks._reap_dev_run(db, p)
        assert p.dev_run_state == "failed"
        assert "unmetered" in p.dev_run_error  # honest about the lost tokens
        assert org.credit_balance == 100.0  # nothing billed
        assert db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == org.id)).scalars().all() == []
        db.rollback()


def test_reaped_project_is_resumable(quiet):
    with SyncSession() as db:
        org = _org(db)
        p = _project(db, org)
        tasks._reap_dev_run(db, p)
        db.flush()
        enabled, blocker = dev_resume_capability(p)
        assert enabled and blocker is None
        db.rollback()
