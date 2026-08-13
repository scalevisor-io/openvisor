"""§auto_dev - the Curated AI auto-developer kind: creation rules (repo + watch
filters mandatory, born in `development`, no platform provisioning), the issue
sweep (filter matching, dedup on source issue URL, daily cap, credit pause with
once-per-24h notify, one-run serialization), the handle_request MVP-gate
exemption, and the PR-link comment back on the triggering issue.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.core.security import hash_password
from app.main import app
from app.models import (
    CreditTransaction, DevRun, IssueWatchEvent, Message, OnboardingAnswer,
    Organization, Project, ProjectMemory, ProjectRepo, Request, StatusChange, User,
)
from app.services import events
from app.workers import tasks


@pytest.fixture
def quiet(monkeypatch):
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)
    return ws


@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="AutoDev Org", credit_balance=100.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                # dev_request_id references request rows - clear it before deleting them
                db.execute(update(Project).where(Project.id.in_(pids))
                           .values(dev_request_id=None))
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
                db.execute(delete(ProjectMemory).where(ProjectMemory.project_id.in_(pids)))
                db.execute(delete(IssueWatchEvent).where(IssueWatchEvent.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _project(oid, **kw):
    kw.setdefault("name", "Sentinel")
    kw.setdefault("description", "Follow OCPA; small PRs.")
    kw.setdefault("kind", "auto_dev")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "idle")
    kw.setdefault("issue_watch", {"labels": ["ai"], "assignees": [], "authors": []})
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.flush()
        db.add(ProjectRepo(project_id=p.id, ssh_uri="git@github.com:acme/app.git",
                           role="primary", provider="github", is_push_target=True))
        db.add(ProjectMemory(project_id=p.id, author="customer", key="GITHUB_TOKEN",
                             value_enc=encrypt("ghp_test"), is_secret=True))
        db.commit()
        return p.id


def _issue(iid, title="Add healthcheck", labels=("ai",), assignees=(), author="alice"):
    return {"iid": iid, "url": f"https://github.com/acme/app/issues/{iid}",
            "title": title, "body": "please", "labels": list(labels),
            "assignees": list(assignees), "author": author}


# ---------------------------------------------------------------- filters

def test_issue_matches_filters():
    m = tasks._issue_matches
    assert m({"labels": ["ai"]}, _issue(1, labels=("ai", "bug")))
    assert not m({"labels": ["ai"]}, _issue(1, labels=("bug",)))
    assert m({"assignees": ["bot"]}, _issue(1, labels=(), assignees=("bot",)))
    # Both set: both must match (label AND assignee, each any-of).
    assert not m({"labels": ["ai"], "assignees": ["bot"]}, _issue(1, labels=("ai",)))
    # Author allowlist restricts, never triggers by itself.
    assert not m({"labels": ["ai"], "authors": ["boss"]}, _issue(1, author="alice"))
    assert m({"labels": ["ai"], "authors": ["alice"]}, _issue(1, author="alice"))
    # No trigger configured -> watch NOTHING, never everything.
    assert not m({}, _issue(1))
    assert not m({"authors": ["alice"]}, _issue(1, author="alice"))


# ---------------------------------------------------------------- sweep

def test_sweep_creates_request_and_dispatches(org, quiet, monkeypatch):
    pid = _project(org)
    monkeypatch.setattr(tasks.github, "list_open_issues",
                        lambda owner, repo, token=None: [_issue(7), _issue(8, labels=("bug",))])
    started: list = []
    monkeypatch.setattr(tasks.handle_request, "apply_async",
                        lambda args=None, **k: started.append(args))
    tasks.auto_dev_issue_sweep()
    with SyncSession() as db:
        reqs = db.query(Request).filter_by(project_id=pid).all()
        assert len(reqs) == 1
        req = reqs[0]
        assert req.source_issue_iid == 7
        assert req.source_issue_url.endswith("/issues/7")
        assert req.handling == "ai" and req.status == "open"
        seed = db.query(Message).filter_by(project_id=pid,
                                           thread=f"request:{req.id}").first()
        assert seed is not None and "issues/7" in seed.body
    assert [a for a in started if a and a[0] == pid] == [[pid, req.id, ""]]


def test_sweep_records_unpollable_watch_once(org, quiet, monkeypatch):
    """A watch that cannot poll (no resolvable API token - e.g. the token saved
    under the wrong Memory key, prod regression) must say so in the history,
    once per 24h, instead of an eternal silent "Nothing yet"."""
    pid = _project(org)
    monkeypatch.setattr(tasks, "_project_repo_token", lambda db, project, provider: None)

    class FakeRedis:
        def __init__(self):
            self.keys: set = set()
        def set(self, key, val, nx=False, ex=None):
            if nx and key in self.keys:
                return None
            self.keys.add(key)
            return True

    fake = FakeRedis()
    monkeypatch.setattr(events, "get_sync_redis", lambda: fake)
    tasks.auto_dev_issue_sweep()
    tasks.auto_dev_issue_sweep()  # second pass inside 24h: marker set, no duplicate
    with SyncSession() as db:
        rows = db.query(IssueWatchEvent).filter_by(project_id=pid, kind="unpollable").all()
        assert len(rows) == 1
        assert "GITHUB_TOKEN" in rows[0].detail
        assert db.query(Request).filter_by(project_id=pid).count() == 0


def test_sweep_dedups_and_serializes(org, quiet, monkeypatch):
    pid = _project(org, dev_run_state="running")
    with SyncSession() as db:
        db.add(Request(project_id=pid, type="feature", handling="ai", status="in_progress",
                       title="dup", source_issue_iid=7,
                       source_issue_url="https://github.com/acme/app/issues/7"))
        db.commit()
    monkeypatch.setattr(tasks.github, "list_open_issues",
                        lambda owner, repo, token=None: [_issue(7)])
    started: list = []
    monkeypatch.setattr(tasks.handle_request, "apply_async",
                        lambda args=None, **k: started.append(args))
    tasks.auto_dev_issue_sweep()
    with SyncSession() as db:
        assert db.query(Request).filter_by(project_id=pid).count() == 1  # no duplicate
    assert [a for a in started if a and a[0] == pid] == []  # run in flight - nothing dispatched


def test_sweep_daily_cap(org, quiet, monkeypatch):
    pid = _project(org)
    monkeypatch.setattr(tasks.settings, "auto_dev_daily_max_starts", 1)
    monkeypatch.setattr(tasks.github, "list_open_issues",
                        lambda owner, repo, token=None: [_issue(1), _issue(2)])
    monkeypatch.setattr(tasks.handle_request, "apply_async", lambda args=None, **k: None)
    tasks.auto_dev_issue_sweep()
    with SyncSession() as db:
        assert db.query(Request).filter_by(project_id=pid).count() == 1


def test_sweep_records_watch_events(org, quiet, monkeypatch):
    """§auto_dev history: registered/deferred/started rows, with `deferred`
    deduped to one row per issue per UTC day across repeated sweeps."""
    pid = _project(org)
    monkeypatch.setattr(tasks.settings, "auto_dev_daily_max_starts", 1)
    monkeypatch.setattr(tasks.github, "list_open_issues",
                        lambda owner, repo, token=None: [_issue(1), _issue(2)])
    monkeypatch.setattr(tasks.handle_request, "apply_async", lambda args=None, **k: None)
    tasks.auto_dev_issue_sweep()
    tasks.auto_dev_issue_sweep()  # the capped issue stays capped - no duplicate row
    with SyncSession() as db:
        evs = db.query(IssueWatchEvent).filter_by(project_id=pid).all()
        by_kind: dict = {}
        for e in evs:
            by_kind.setdefault(e.kind, []).append(e)
        assert len(by_kind["registered"]) == 1
        reg = by_kind["registered"][0]
        assert reg.issue_url.endswith("/issues/1")
        assert reg.issue_title == "Add healthcheck"
        assert reg.request_id == db.query(Request).filter_by(project_id=pid).one().id
        assert len(by_kind["deferred"]) == 1
        assert by_kind["deferred"][0].issue_url.endswith("/issues/2")
        # dispatched once per sweep while the request stays open with no run in flight
        assert len(by_kind["started"]) == 2
        assert by_kind["started"][0].request_id == reg.request_id


def test_sweep_out_of_credits_notifies_once(org, quiet, monkeypatch):
    pid = _project(org)
    with SyncSession() as db:
        db.get(Organization, org).credit_balance = 0.0
        db.add(User(org_id=org, email=f"own-{uuid.uuid4().hex[:8]}@example.com",
                    password_hash=hash_password("x-secret-123"), role="customer",
                    email_verified=True))
        db.commit()
    monkeypatch.setattr(tasks.github, "list_open_issues",
                        lambda owner, repo, token=None: [_issue(9)])

    class FakeRedis:
        def __init__(self):
            self.keys: set = set()
        def set(self, key, val, nx=False, ex=None):
            if nx and key in self.keys:
                return None
            self.keys.add(key)
            return True

    fake = FakeRedis()
    monkeypatch.setattr(events, "get_sync_redis", lambda: fake)
    sent: list = []
    monkeypatch.setattr(tasks.emailer, "send_email", lambda *a, **k: sent.append(a) or True)
    tasks.auto_dev_issue_sweep()
    tasks.auto_dev_issue_sweep()  # second pass: marker set -> no re-notify
    with SyncSession() as db:
        assert db.query(Request).filter_by(project_id=pid).count() == 0  # paused, no request
        notes = db.query(Message).filter_by(project_id=pid, thread="main").all()
        assert len(notes) == 1 and "credit balance is empty" in notes[0].body
        # history: one `paused` row, throttled with the notice
        paused = db.query(IssueWatchEvent).filter_by(project_id=pid, kind="paused").all()
        assert len(paused) == 1
    assert len(sent) == 1


def test_sweep_ignores_unsupported_or_tokenless(org, quiet, monkeypatch):
    pid = _project(org)
    with SyncSession() as db:
        db.execute(delete(ProjectMemory).where(ProjectMemory.project_id == pid))
        db.commit()
    monkeypatch.setattr(tasks.settings, "github_token", "", raising=False)
    called: list = []
    monkeypatch.setattr(tasks.github, "list_open_issues",
                        lambda *a, **k: called.append(1) or [])
    tasks.auto_dev_issue_sweep()
    assert called == []  # no token -> never polls


# ---------------------------------------------------------------- handle_request

def test_handle_request_auto_dev_skips_mvp_gate(org, quiet, monkeypatch):
    pid = _project(org, demo_deployed_once=False)
    with SyncSession() as db:
        req = Request(project_id=pid, type="feature", handling="ai", status="open",
                      title="Add healthcheck", source_issue_iid=7,
                      source_issue_url="https://github.com/acme/app/issues/7")
        db.add(req)
        db.commit()
        rid = req.id
    runs: list = []
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda args=None, **k: runs.append(args))
    tasks.handle_request(pid, rid, "")
    with SyncSession() as db:
        assert db.get(Request, rid).status == "in_progress"
        assert db.get(Project, pid).dev_request_id == rid
    assert runs == [[pid]]


def test_handle_request_ai_still_needs_mvp(org, quiet, monkeypatch):
    pid = _project(org, kind="ai", demo_deployed_once=False)
    with SyncSession() as db:
        req = Request(project_id=pid, type="feature", handling="ai", status="open",
                      title="x")
        db.add(req)
        db.commit()
        rid = req.id
    runs: list = []
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda args=None, **k: runs.append(args))
    tasks.handle_request(pid, rid, "")
    with SyncSession() as db:
        assert db.get(Request, rid).status == "open"
    assert runs == []


# ---------------------------------------------------------------- issue comment

def test_comment_source_issue_github(org, quiet, monkeypatch):
    pid = _project(org)
    with SyncSession() as db:
        project = db.get(Project, pid)
        project.dev_pr_url = "https://github.com/acme/app/pull/12"
        req = Request(project_id=pid, type="feature", handling="ai", status="in_progress",
                      title="t", source_issue_iid=7,
                      source_issue_url="https://github.com/acme/app/issues/7")
        db.add(req)
        db.commit()
        posted: list = []
        monkeypatch.setattr(tasks.github, "create_issue_comment",
                            lambda owner, repo, number, body, token=None:
                            posted.append((owner, repo, number, body, token)))
        target = {"provider": "github", "owner": "acme", "repo": "app"}
        tasks._comment_source_issue(db, project, target, req)
    assert len(posted) == 1
    owner, repo, number, body, token = posted[0]
    assert (owner, repo, number) == ("acme", "app", 7)
    assert "pull/12" in body and token == "ghp_test"


def test_comment_source_issue_summary_only_when_enabled(org, quiet, monkeypatch):
    # summarize_to_issue appends the run's work summary to the PR-link comment;
    # off (or an absent key, e.g. the platform fallback) posts the link alone
    pid = _project(org)
    with SyncSession() as db:
        project = db.get(Project, pid)
        project.dev_pr_url = "https://github.com/acme/app/pull/13"
        req = Request(project_id=pid, type="feature", handling="ai", status="in_progress",
                      title="t", source_issue_iid=8,
                      source_issue_url="https://github.com/acme/app/issues/8",
                      work_summary="Added the widget endpoint and its tests.")
        db.add(req)
        db.commit()
        posted: list = []
        monkeypatch.setattr(tasks.github, "create_issue_comment",
                            lambda owner, repo, number, body, token=None:
                            posted.append(body))
        target = {"provider": "github", "owner": "acme", "repo": "app"}
        tasks._comment_source_issue(db, project, target, req)
        tasks._comment_source_issue(db, project, {**target, "summarize_to_issue": True}, req)
    assert "widget endpoint" not in posted[0]
    assert "pull/13" in posted[1] and "widget endpoint" in posted[1]


def test_comment_failure_lands_in_watch_history(org, quiet, monkeypatch):
    # A token that pushes and opens PRs but cannot write issue comments 403s
    # ONLY here - the failure must reach the Issue-watch history, not just a
    # worker log line that dies with the pod.
    pid = _project(org)
    with SyncSession() as db:
        project = db.get(Project, pid)
        project.dev_pr_url = "https://github.com/acme/app/pull/14"
        req = Request(project_id=pid, type="feature", handling="ai", status="in_progress",
                      title="t", source_issue_iid=9,
                      source_issue_url="https://github.com/acme/app/issues/9")
        db.add(req)
        db.commit()

        def boom(*a, **k):
            raise RuntimeError("403 Forbidden for url .../issues/9/comments")

        monkeypatch.setattr(tasks.github, "create_issue_comment", boom)
        target = {"provider": "github", "owner": "acme", "repo": "app"}
        tasks._comment_source_issue(db, project, target, req)
        db.commit()
        rows = db.query(IssueWatchEvent).filter_by(project_id=pid, kind="comment_failed").all()
        assert len(rows) == 1
        ev = rows[0]
        assert ev.request_id == req.id
        assert ev.issue_url == "https://github.com/acme/app/issues/9"
        assert "403" in ev.detail and "issue-write permission" in ev.detail

        # a non-auto_dev project keeps the old best-effort silence
        project.kind = "ai"
        tasks._comment_source_issue(db, project, target, req)
        db.commit()
        assert db.query(IssueWatchEvent).filter_by(project_id=pid,
                                                   kind="comment_failed").count() == 1


# ---------------------------------------------------------------- HTTP creation

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _login(client):
    email = f"autodev-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "autodev-secret-1"
    with SyncSession() as db:
        org = Organization(name="AutoDev HTTP Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        db.commit()
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_create_auto_dev_rules(client, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    sent: list = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: sent.append(a))
    h = _login(client)
    base = {"kind": "auto_dev", "speciality": "general-webapp",
            "description": "Small PRs, OCPA, tests first.", "from_scratch": False,
            "sovereign": False}

    r = client.post("/api/projects", json=base, headers=h)
    assert r.status_code == 400  # no repo

    r = client.post("/api/projects", json={
        **base, "repos": [{"ssh_uri": "git@bitbucket.org:acme/app.git"}],
        "issue_watch": {"labels": ["ai"], "assignees": [], "authors": []}}, headers=h)
    assert r.status_code == 400  # unsupported provider

    r = client.post("/api/projects", json={
        **base, "repos": [{"ssh_uri": "git@github.com:acme/app.git"}]}, headers=h)
    assert r.status_code == 400  # no watch filters

    r = client.post("/api/projects", json={
        **base, "repos": [{"ssh_uri": "git@github.com:acme/app.git"}],
        "issue_watch": {"labels": ["ai"], "assignees": [], "authors": []}}, headers=h)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["kind"] == "auto_dev"
    assert p["status"] == "development"  # born watching - no evaluation/payment
    assert p["issue_watch"] == {"labels": ["ai"], "assignees": [], "authors": []}
    assert p["ssh_public_key"]  # sandbox scaffolding generated
    assert not any(a and a[0] == "app.workers.tasks.provision_project" for a in sent)

    # The standing policy (description) is editable in ANY status for auto_dev...
    r = client.patch(f"/api/projects/{p['id']}",
                     json={"description": "Bigger PRs are fine."}, headers=h)
    assert r.status_code == 200 and r.json()["description"] == "Bigger PRs are fine."
    # ...and so is the issue watch (with the same at-least-one-trigger rule).
    r = client.patch(f"/api/projects/{p['id']}",
                     json={"issue_watch": {"labels": [], "assignees": [], "authors": []}},
                     headers=h)
    assert r.status_code == 400
    r = client.patch(f"/api/projects/{p['id']}",
                     json={"issue_watch": {"labels": ["auto"], "assignees": ["bot"],
                                           "authors": ["alice"]}}, headers=h)
    assert r.status_code == 200
    assert r.json()["issue_watch"]["assignees"] == ["bot"]

    # ...and so are the onboarding answers: the wizard saves them right after
    # create (the project is already in `development`, never draft), and they
    # feed every dev run's context.
    r = client.post(f"/api/projects/{p['id']}/answers",
                    json={"answers": [{"question_id": "project_type",
                                       "option_ids": ["infra_devops"]}]}, headers=h)
    assert r.status_code == 200, r.text

    # Hermetic: drop the created sentinel so the beat sweep (and later test runs)
    # never pick it up as a stray auto_dev project.
    with SyncSession() as db:
        db.execute(delete(OnboardingAnswer).where(OnboardingAnswer.project_id == p["id"]))
        db.execute(delete(Message).where(Message.project_id == p["id"]))
        db.execute(delete(StatusChange).where(StatusChange.project_id == p["id"]))
        db.execute(delete(ProjectRepo).where(ProjectRepo.project_id == p["id"]))
        db.execute(delete(Project).where(Project.id == p["id"]))
        db.commit()


def test_issue_events_endpoint(client, monkeypatch):
    from datetime import timedelta

    from app.models import utcnow
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    h = _login(client)

    r = client.post("/api/projects", json={
        "kind": "auto_dev", "speciality": "general-webapp",
        "description": "Small PRs.", "from_scratch": False, "sovereign": False,
        "repos": [{"ssh_uri": "git@github.com:acme/app.git"}],
        "issue_watch": {"labels": ["ai"], "assignees": [], "authors": []}}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    # auto_dev only: any other kind answers 409.
    r = client.post("/api/projects", json={
        "kind": "ai", "speciality": "general-webapp", "description": "An app.",
        "from_scratch": True, "sovereign": False}, headers=h)
    assert r.status_code == 201, r.text
    ai_pid = r.json()["id"]
    assert client.get(f"/api/projects/{ai_pid}/issue-events").status_code == 409

    base = utcnow()
    with SyncSession() as db:
        for i in range(25):
            db.add(IssueWatchEvent(
                project_id=pid, kind="registered",
                issue_url=f"https://github.com/acme/app/issues/{i}",
                issue_title=f"Issue {i}", created_at=base + timedelta(seconds=i)))
        db.commit()

    r = client.get(f"/api/projects/{pid}/issue-events?limit=10&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 25 and len(body["events"]) == 10
    assert body["events"][0]["issue_title"] == "Issue 24"  # newest first
    assert body["events"][9]["issue_title"] == "Issue 15"
    r = client.get(f"/api/projects/{pid}/issue-events?limit=10&offset=20")
    tail = r.json()
    assert tail["total"] == 25 and len(tail["events"]) == 5
    assert tail["events"][-1]["issue_title"] == "Issue 0"

    with SyncSession() as db:
        for xid in (pid, ai_pid):
            db.execute(delete(IssueWatchEvent).where(IssueWatchEvent.project_id == xid))
            db.execute(delete(Message).where(Message.project_id == xid))
            db.execute(delete(StatusChange).where(StatusChange.project_id == xid))
            db.execute(delete(ProjectRepo).where(ProjectRepo.project_id == xid))
            db.execute(delete(DevRun).where(DevRun.project_id == xid))
            db.execute(delete(Request).where(Request.project_id == xid))
            db.execute(delete(Project).where(Project.id == xid))
        db.commit()


def test_slot_refusal_message_not_repeated(org, quiet, monkeypatch):
    """The sweep retries a queued request every minute; a SlotRefused must not
    re-post the same busy copy each pass (prod regression: one per minute)."""
    pid = _project(org)
    with SyncSession() as db:
        req = Request(project_id=pid, type="feature", handling="ai", status="open",
                      title="Queued work", source_issue_url="https://github.com/acme/app/issues/9")
        db.add(req)
        db.commit()
        rid = req.id

    def refuse(db, project, request=None, predecessor=None):
        raise tasks.dev_concurrency.SlotRefused(
            "A build is already in progress for this project - re-submit this "
            "request once it completes.")
    monkeypatch.setattr(tasks.dev_concurrency, "acquire_slot", refuse)
    tasks.handle_request(pid, rid, "")
    tasks.handle_request(pid, rid, "")
    with SyncSession() as db:
        msgs = db.query(Message).filter_by(project_id=pid, thread=f"request:{rid}",
                                           author="agent").all()
        busy = [m for m in msgs if "already in progress" in m.body]
        assert len(busy) == 1
