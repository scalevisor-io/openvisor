"""§delivery reconciler: the state of a request's change is OBSERVED on the
repository every tick and before every Resume, and the next action is derived
from it - never from a pointer a run's exit path happened to stamp.

Prod, 2026-09-03: a `ci_timeout` park dropped the run out of the merge sweep,
GitLab merged the MR six minutes later, and two Start-fresh rebuilds followed
(one of them correctly concluding "no changes", which the platform reported as a
failure). Every case below is a shape of that night.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.models import (
    DeploymentEvent, DevRun, Message, Organization, Project, Request, StatusChange,
)
from app.services import delivery, dev_concurrency, events
from app.services.delivery import Change, Snapshot, decide
from app.workers import tasks

WEB = "https://git.example.com/grp/proj"


# ---------------------------------------------------------------- decide (pure)

def _snap(changes=(), **kw):
    kw.setdefault("provider", "gitlab")
    kw.setdefault("platform", True)
    kw.setdefault("target", {"provider": "gitlab", "customer": False})
    return Snapshot(changes=list(changes), **kw)


def _mr(number=2, state="open", **kw):
    kw.setdefault("url", f"{WEB}/-/merge_requests/{number}")
    kw.setdefault("branch", "feat/x")
    kw.setdefault("provider", "gitlab")
    return Change(number=number, state=state, **kw)


def test_decide_merged_change_deploys_even_after_a_failed_park():
    v = decide(_snap([_mr(state="merged")], latest_run_state="failed"),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "deploy" and v.change.number == 2


def test_decide_merged_pr_deliverable_finalizes():
    v = decide(_snap([_mr(state="merged")], latest_run_state="failed"),
               request_status="in_progress", pr_deliverable=True)
    assert v.action == "finalize"


def test_decide_delivered_request_is_idle():
    v = decide(_snap([_mr(state="merged")], latest_run_state="done"),
               request_status="done", pr_deliverable=False)
    assert v.action == "idle"


def test_decide_live_run_waits_before_anything():
    v = decide(_snap([_mr(state="merged")], live_run_state="running"),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "wait"


def test_decide_newest_change_decides_over_an_older_merged_one():
    # MR !3 (a revision) open, MR !2 merged earlier: the open one is the work
    v = decide(_snap([_mr(3, ci="pending"), _mr(2, state="merged")],
                     latest_run_state="failed", ci_available=True),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "arm" and v.change.number == 3


def test_decide_green_or_mergeable_merges():
    assert decide(_snap([_mr(ci="success")]), request_status="in_progress",
                  pr_deliverable=False).action == "merge"
    assert decide(_snap([_mr(ci="none", merge_status="mergeable")]),
                  request_status="in_progress", pr_deliverable=False).action == "merge"


def test_decide_failed_pipeline_fixes():
    v = decide(_snap([_mr(ci="failure")]), request_status="in_progress", pr_deliverable=False)
    assert v.action == "fix_ci" and v.settle


def test_decide_no_ci_at_all_merges_directly():
    v = decide(_snap([_mr(ci="none")], ci_available=False),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "merge"


def test_decide_pipeline_required_but_impossible_parks_as_platform_fault():
    v = decide(_snap([_mr(ci="none", merge_status="ci_must_pass")], ci_available=False),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "park" and v.fault == "platform"


def test_decide_pending_pipeline_with_no_runner_parks_as_platform_fault():
    v = decide(_snap([_mr(ci="pending")], ci_available=False),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "park" and v.fault == "platform" and "runner" in v.note


def test_decide_no_pipeline_yet_waits_within_grace_then_merges():
    fresh = _mr(ci="none", updated_at=datetime.now(timezone.utc))
    assert decide(_snap([fresh], ci_available=True), request_status="in_progress",
                  pr_deliverable=False).action == "wait"
    old = _mr(ci="none", updated_at=datetime.now(timezone.utc) - timedelta(minutes=10))
    assert decide(_snap([old], ci_available=True), request_status="in_progress",
                  pr_deliverable=False).action == "merge"


def test_decide_conflict_parks_for_the_customer_and_gitlab_rules_for_the_admin():
    v = decide(_snap([_mr(merge_status="conflict")]), request_status="in_progress",
               pr_deliverable=False)
    assert v.action == "park" and v.fault is None
    v = decide(_snap([_mr(merge_status="discussions_not_resolved")]),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "park" and v.fault == "platform"


def test_decide_customer_repo_is_watched_never_merged():
    v = decide(_snap([_mr(ci="success", provider="github")], provider="github",
                     platform=False, target={"provider": "github"}),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "wait" and v.settle


def test_decide_closed_change_ends_an_awaiting_merge_unit():
    v = decide(_snap([_mr(state="closed")], latest_run_state="awaiting_merge"),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "reject"
    v = decide(_snap([_mr(state="closed")], latest_run_state="failed"),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "idle"


def test_decide_nothing_on_the_repository_is_idle_unless_pushed_and_waiting():
    assert decide(_snap(latest_run_state="failed"), request_status="in_progress",
                  pr_deliverable=False).action == "idle"
    assert decide(_snap(latest_run_state="awaiting_merge"), request_status="in_progress",
                  pr_deliverable=False).action == "wait"
    assert decide(_snap(error="boom"), request_status="in_progress",
                  pr_deliverable=False).action == "wait"


def test_verdict_key_is_stable_for_the_same_situation():
    a = decide(_snap([_mr(ci="pending")], ci_available=True), request_status="in_progress",
               pr_deliverable=False)
    b = decide(_snap([_mr(ci="pending")], ci_available=True), request_status="in_progress",
               pr_deliverable=False)
    assert a.key == b.key


# ---------------------------------------------------------------- observe

def test_observe_finds_a_change_by_branch_with_every_pointer_cleared(monkeypatch):
    seen = []
    monkeypatch.setattr(delivery.gitlab, "list_mrs_for_branch",
                        lambda gid, br: seen.append(br) or ([{
                            "iid": 2, "state": "merged", "source_branch": br,
                            "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc"}]
                            if br == "feat/x" else []))
    snap = delivery.observe(target={"provider": "gitlab", "customer": False},
                            gitlab_project_id=280, branches=["feat/x", "chore/kb"],
                            pointers=[], token=None, live_run_state=None,
                            latest_run_state="failed")
    assert seen == ["feat/x", "chore/kb"]
    assert snap.newest.state == "merged" and snap.newest.number == 2


def test_observe_is_fail_soft_per_call(monkeypatch):
    def boom(gid, br):
        raise RuntimeError("gitlab down")
    monkeypatch.setattr(delivery.gitlab, "list_mrs_for_branch", boom)
    monkeypatch.setattr(delivery.gitlab, "get_mr",
                        lambda gid, iid: {"iid": iid, "state": "merged", "source_branch": "b"})
    snap = delivery.observe(target={"provider": "gitlab", "customer": False},
                            gitlab_project_id=280, branches=["b"], pointers=[(2, None)],
                            token=None, live_run_state=None, latest_run_state="failed")
    assert snap.newest.number == 2 and snap.error and "gitlab down" in snap.error


def test_observe_skips_the_provider_while_a_build_is_live(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called")
    monkeypatch.setattr(delivery.gitlab, "list_mrs_for_branch", boom)
    snap = delivery.observe(target={"provider": "gitlab", "customer": False},
                            gitlab_project_id=280, branches=["b"], pointers=[(2, None)],
                            token=None, live_run_state="running", latest_run_state="running")
    assert snap.changes == []


def test_observe_github_closed_pr_with_commits_in_base_counts_as_merged(monkeypatch):
    monkeypatch.setattr(delivery.github, "list_prs_for_branch",
                        lambda o, r, b, token=None: [{"number": 7, "state": "closed",
                                                      "merged_at": None,
                                                      "html_url": "https://github.com/a/b/pull/7",
                                                      "head": {"ref": b, "sha": "s7"}}])
    monkeypatch.setattr(delivery.github, "commits_contained_in", lambda *a, **k: True)
    snap = delivery.observe(target={"provider": "github", "owner": "a", "repo": "b"},
                            gitlab_project_id=None, branches=["feat/x"], pointers=[],
                            token="t", live_run_state=None, latest_run_state="awaiting_merge")
    assert snap.newest.state == "merged"


# ---------------------------------------------------------------- the worker, end to end

@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="Reconciler Test Org", credit_balance=100.0)
        db.add(org)
        db.commit()
        oid = org.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                for p in db.execute(select(Project).where(Project.id.in_(pids))).scalars().all():
                    p.dev_request_id = None
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
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)
    return ws


@pytest.fixture
def deployed(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tasks.demo_start, "apply_async",
                        lambda *a, **k: calls.append((a, k)))
    return calls


def _the_night(oid, run_state="failed", status="awaiting_customer"):
    """Prod 662f1fac as the ledger recorded it: request in_progress, the run
    that opened MR !2 superseded by a Start fresh (pointers cleared on the
    project), the fresh run parked failed on "no changes"."""
    with SyncSession() as db:
        p = Project(org_id=oid, name="Let's Match", description="d", kind="ai",
                    status=status, dev_run_state=run_state, workspace_path="/tmp/ws",
                    subdomain="sub-" + uuid.uuid4().hex[:8],
                    gitlab_project_id=280, gitlab_ssh_url="git@git.example.com:grp/proj.git",
                    gitlab_web_url=WEB, dev_branch="chore/kb-maintenance",
                    dev_pr_number=None, dev_pr_url=None)
        db.add(p)
        db.flush()
        req = Request(project_id=p.id, type="mvp", title="Initial build", status="in_progress")
        db.add(req)
        db.flush()
        p.dev_request_id = req.id
        older = DevRun(project_id=p.id, request_id=req.id, state="superseded",
                       branch="feat/session-marketplace-mvp", pr_number=2,
                       pr_url=f"{WEB}/-/merge_requests/2",
                       run_error="Merge blocked: ci_timeout",
                       created_at=datetime.now(timezone.utc) - timedelta(hours=2))
        fresh = DevRun(project_id=p.id, request_id=req.id, state=run_state,
                       branch="chore/kb-maintenance", pr_number=None,
                       run_error="The run produced no changes to publish",
                       created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        db.add_all([older, fresh])
        db.commit()
        return p.id, req.id, older.id


def _messages(pid):
    with SyncSession() as db:
        return [m.body for m in db.execute(select(Message).where(Message.project_id == pid)
                                           .order_by(Message.created_at)).scalars().all()]


def _platform_repo(monkeypatch, mrs_by_branch):
    monkeypatch.setattr(tasks.gitlab, "list_mrs_for_branch",
                        lambda gid, br: mrs_by_branch.get(br, []))
    monkeypatch.setattr(tasks.gitlab, "get_mr", lambda gid, iid: (_ for _ in ()).throw(
        AssertionError("pointer lookup must not be needed")))


def test_merge_that_landed_after_the_park_deploys_on_the_next_tick(org_id, quiet, deployed,
                                                                   monkeypatch):
    pid, rid, older_id = _the_night(org_id)
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "merged", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc"}]})
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        p = db.get(Project, pid)
        req = db.get(Request, rid)
        assert p.dev_run_state == "deploying"
        assert p.dev_pr_number == 2                       # the pointer is re-derived
        assert req.delivery["action"] == "deploy" and req.delivery["change"]["number"] == 2
        revived = db.get(DevRun, older_id)
        assert revived.state == "deploying"                # the row that owns MR !2
        # its clock restarted with it: the row-level reaper judges a deploying
        # row by its own started_at, and this one used to read hours old
        assert revived.started_at >= datetime.now(timezone.utc) - timedelta(minutes=1)
    assert deployed and deployed[0][1]["kwargs"]["run_id"] == older_id
    assert any("!2 was merged" in m for m in _messages(pid))
    # The deploy finished (demo_start marks the owning row done). The newest
    # row of the request is still the failed one - the next tick must read the
    # `done` row that carries MR !2 as the delivery, not redeploy every minute.
    with SyncSession() as db:
        db.get(DevRun, older_id).state = "done"
        p = db.get(Project, pid)
        p.dev_run_state = "done"
        db.commit()
    tasks.dev_pr_sweep()
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(Request, rid).delivery["action"] == "idle"
        assert db.get(Project, pid).dev_run_state == "done"
    assert len(deployed) == 1
    assert sum("!2 was merged" in m for m in _messages(pid)) == 1


def test_decide_a_done_row_carrying_the_change_means_delivered():
    v = decide(_snap([_mr(state="merged")], latest_run_state="failed",
                     delivered_numbers=frozenset({2})),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "idle" and v.note == "delivered"


def test_open_change_after_a_failed_park_is_watched_not_rebuilt(org_id, quiet, deployed,
                                                                monkeypatch):
    pid, rid, _older = _the_night(org_id)
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "opened", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc",
        "detailed_merge_status": "ci_still_running", "merge_when_pipeline_succeeds": False}]})
    monkeypatch.setattr(tasks.gitlab, "pipeline_for_sha", lambda gid, sha: {"status": "running"})
    monkeypatch.setattr(tasks.gitlab, "ci_available", lambda gid: True)
    armed = []
    monkeypatch.setattr(tasks.gitlab, "merge_now",
                        lambda gid, iid, squash=True, when_pipeline_succeeds=False:
                        armed.append(when_pipeline_succeeds) or (False, "armed"))
    tasks.dev_pr_sweep()
    tasks.dev_pr_sweep()   # the same situation twice: announced once, armed once
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"
        assert db.get(Request, rid).delivery["action"] == "arm"
    assert armed == [True]
    assert sum("!2 is open" in m for m in _messages(pid)) == 1
    assert deployed == []


def test_merge_refusal_parks_once_with_gitlabs_words(org_id, quiet, deployed, monkeypatch):
    pid, rid, _older = _the_night(org_id)
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "opened", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc",
        "detailed_merge_status": "ci_must_pass"}]})
    monkeypatch.setattr(tasks.gitlab, "pipeline_for_sha", lambda gid, sha: None)
    monkeypatch.setattr(tasks.gitlab, "ci_available", lambda gid: False)
    tasks.dev_pr_sweep()
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.status == "awaiting_admin"
        assert p.dev_run_state == "failed" and p.dev_run_fault == "platform"
        assert "no pipeline can run" in (p.dev_run_error or "")
    parks = [m for m in _messages(pid) if "can't be merged" in m]
    assert len(parks) == 1 and "no pipeline can run" in parks[0]
    assert deployed == []


def test_direct_merge_when_the_repo_has_no_ci(org_id, quiet, deployed, monkeypatch):
    pid, rid, older_id = _the_night(org_id)
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "opened", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc",
        "detailed_merge_status": "mergeable"}]})
    monkeypatch.setattr(tasks.gitlab, "pipeline_for_sha", lambda gid, sha: None)
    monkeypatch.setattr(tasks.gitlab, "ci_available", lambda gid: False)
    merged = []
    monkeypatch.setattr(tasks.gitlab, "merge_now",
                        lambda gid, iid, squash=True, when_pipeline_succeeds=False:
                        merged.append(iid) or (True, "merged"))
    tasks.dev_pr_sweep()
    assert merged == [2]
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
    assert deployed and deployed[0][1]["kwargs"]["run_id"] == older_id


def test_failed_pipeline_chains_a_fix_from_a_failed_park(org_id, quiet, deployed, monkeypatch):
    pid, rid, older_id = _the_night(org_id)
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "opened", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc"}]})
    monkeypatch.setattr(tasks.gitlab, "pipeline_for_sha", lambda gid, sha: {"status": "failed"})
    monkeypatch.setattr(tasks.gitlab, "failed_pipeline_logs", lambda gid, iid: "npm ERR! boom")
    dispatched = []
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **k: dispatched.append(k))
    monkeypatch.setattr(tasks, "_refresh_root_workspace", lambda *a, **k: None, raising=False)
    tasks.dev_pr_sweep()
    assert len(dispatched) == 1 and "npm ERR! boom" in dispatched[0]["kwargs"]["fix_instruction"]
    with SyncSession() as db:
        fix = db.get(DevRun, dispatched[0]["kwargs"]["run_id"])
        assert fix.predecessor_id == older_id and fix.pr_number == 2
        assert fix.ci_fix_attempts == 1


def test_resume_on_a_merged_change_deploys_instead_of_spending_a_slot(org_id, quiet, deployed,
                                                                      monkeypatch):
    pid, rid, older_id = _the_night(org_id)
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "merged", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc"}]})
    out = tasks.delivery_gate(pid, rid, fresh=False)
    assert out["handled"] and "already merged" in out["message"]
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
        assert db.execute(select(DevRun).where(DevRun.project_id == pid,
                                               DevRun.state == "queued")).first() is None
    assert deployed


def test_resume_on_an_open_change_is_refused_with_the_reason(org_id, quiet, deployed,
                                                             monkeypatch):
    pid, rid, _older = _the_night(org_id)
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "opened", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc",
        "merge_when_pipeline_succeeds": True}]})
    monkeypatch.setattr(tasks.gitlab, "pipeline_for_sha", lambda gid, sha: {"status": "running"})
    monkeypatch.setattr(tasks.gitlab, "ci_available", lambda gid: True)
    out = tasks.delivery_gate(pid, rid, fresh=False)
    assert out["blocked"] and "!2 is already open" in out["message"]
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "awaiting_merge"
    assert deployed == []


def test_start_fresh_closes_the_open_change_then_builds(org_id, quiet, deployed, monkeypatch):
    pid, rid, _older = _the_night(org_id)
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "opened", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc"}]})
    monkeypatch.setattr(tasks.gitlab, "pipeline_for_sha", lambda gid, sha: {"status": "running"})
    monkeypatch.setattr(tasks.gitlab, "ci_available", lambda gid: True)
    closed = []
    monkeypatch.setattr(tasks.gitlab, "close_mr", lambda gid, iid: closed.append(iid))
    assert tasks.delivery_gate(pid, rid, fresh=True) == {}
    assert closed == [2]
    assert any("closed merge request !2" in m for m in _messages(pid))
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "failed"  # untouched: the build follows


def test_nothing_on_the_repository_lets_the_build_proceed(org_id, quiet, deployed, monkeypatch):
    pid, rid, _older = _the_night(org_id)
    _platform_repo(monkeypatch, {})
    assert tasks.delivery_gate(pid, rid, fresh=False) == {}
    assert deployed == []


def test_no_changes_exit_adopts_a_merged_change_found_by_branch(org_id, quiet, deployed,
                                                                 monkeypatch):
    """Probe 0 on the run's own exit path: the fresh run that produced nothing
    because main already held the work is a delivery, not a failure."""
    pid, rid, older_id = _the_night(org_id, run_state="running", status="development")
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "merged", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc"}]})
    with SyncSession() as db:
        p = db.get(Project, pid)
        fresh = db.execute(select(DevRun).where(DevRun.project_id == pid,
                                                DevRun.state == "running")).scalar_one()
        dev_concurrency.bind_run(p, fresh)
        assert tasks._adopt_merged_change(db, p, f"request:{rid}", "logs") is True
        db.commit()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
    assert deployed


# ---------------------------------------------------------------- selection + holds

def test_decide_done_run_means_delivered_for_an_mvp_request():
    # Request #0 stays in_progress until the project finishes: the run's `done`
    # is the delivery marker, so a merged MVP is never redeployed by the tick.
    v = decide(_snap([_mr(state="merged")], latest_run_state="done"),
               request_status="in_progress", pr_deliverable=False)
    assert v.action == "idle"


def test_decide_consultant_hold_watches_but_never_merges():
    v = decide(_snap([_mr(ci="success", merge_status="mergeable")]),
               request_status="in_progress", pr_deliverable=False,
               project_status="awaiting_admin")
    assert v.action == "wait" and "consultant" in v.note
    # a change that already landed still deploys under the hold
    v = decide(_snap([_mr(state="merged")], latest_run_state="failed"),
               request_status="in_progress", pr_deliverable=False,
               project_status="awaiting_admin")
    assert v.action == "deploy"


def test_tick_skips_finished_projects_and_stale_requests(org_id, quiet, deployed, monkeypatch):
    observed = []
    monkeypatch.setattr(tasks.gitlab, "list_mrs_for_branch",
                        lambda gid, br: observed.append(gid) or [])
    pid_recent, _r, _o = _the_night(org_id)
    pid_finished, _r, _o = _the_night(org_id, status="finished")
    pid_stale, rid_stale, _o = _the_night(org_id)
    with SyncSession() as db:
        for row in db.execute(select(DevRun).where(DevRun.project_id == pid_stale)).scalars():
            row.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()
    tasks.dev_pr_sweep()
    assert observed and set(observed) == {280}
    with SyncSession() as db:
        # only the recent live project was observed and recorded
        assert db.get(Request, rid_stale).delivery is None
        recent = db.execute(select(Request).where(Request.project_id == pid_recent)).scalar_one()
        assert recent.delivery is not None
        finished = db.execute(select(Request).where(Request.project_id == pid_finished)).scalar_one()
        assert finished.delivery is None


def test_tick_keeps_watching_an_awaiting_merge_row_whatever_its_age(org_id, quiet, deployed,
                                                                     monkeypatch):
    pid, rid, older_id = _the_night(org_id, run_state="awaiting_merge")
    with SyncSession() as db:
        for row in db.execute(select(DevRun).where(DevRun.project_id == pid)).scalars():
            row.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()
    _platform_repo(monkeypatch, {"feat/session-marketplace-mvp": [{
        "iid": 2, "state": "merged", "source_branch": "feat/session-marketplace-mvp",
        "web_url": f"{WEB}/-/merge_requests/2", "sha": "abc"}]})
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
    assert deployed


def test_mirror_pointer_of_another_request_is_never_this_requests_change(org_id, quiet,
                                                                          deployed, monkeypatch):
    pid, rid, _older = _the_night(org_id)
    with SyncSession() as db:
        p = db.get(Project, pid)
        other = Request(project_id=pid, type="feature", title="other", status="in_progress")
        db.add(other)
        db.flush()
        p.dev_request_id = other.id          # the mirror names the OTHER request
        p.dev_pr_number = 9
        p.dev_pr_url = f"{WEB}/-/merge_requests/9"
        # the other request must not be swept itself (no run rows -> not selected)
        db.commit()
    looked = []
    monkeypatch.setattr(tasks.gitlab, "list_mrs_for_branch", lambda gid, br: [])
    monkeypatch.setattr(tasks.gitlab, "get_mr",
                        lambda gid, iid: looked.append(iid) or {"iid": iid, "state": "merged"})
    tasks.dev_pr_sweep()
    assert 9 not in looked            # the mirror's pointer stayed the other request's
    assert looked == [2]              # the request's own row pointer still resolves


def test_resume_while_a_deploy_is_in_flight_is_refused(org_id, quiet, deployed, monkeypatch):
    """The tick found the change merged and is deploying it; a Resume clicked in
    that window must not spend a sandbox (prod: it did, while the reaper had
    momentarily re-labelled the deploying row as failed)."""
    pid, rid, older_id = _the_night(org_id, run_state="failed")
    with SyncSession() as db:
        db.get(DevRun, older_id).state = "deploying"
        db.commit()
    _platform_repo(monkeypatch, {})
    out = tasks.delivery_gate(pid, rid, fresh=False)
    assert out["blocked"] and "in flight" in out["message"]
    assert deployed == []
