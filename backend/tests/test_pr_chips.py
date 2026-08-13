"""§PR chips: structured PR/MR references - `_pr_ref` validation, per-request
accumulation (`Request.pr_urls`, dedup by url across re-runs), the request
serializer field, and the Message.meta.prs payload the worker attaches wherever
a chat message references a change."""
import pytest
from sqlalchemy import delete, select, update

from app.api.serializers import request_out
from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.models import (
    CreditTransaction, Message, Organization, Project, ProjectMemory, ProjectRepo,
    Request, StatusChange,
)
from app.services import events
from app.workers import tasks
from app.workers.celery_app import celery

GL_REMOTE = "git@gitlab.com:acme/widgets.git"
GL_TARGET = {"provider": "gitlab", "customer": True, "remote": GL_REMOTE,
             "runner_provider": "gitlab_customer", "base_url": "https://gitlab.com",
             "path": "acme/widgets", "base_branch": "main", "auto_merge": True,
             "repo_id": None}


@pytest.fixture
def quiet(monkeypatch):
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(celery, "send_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: None)
    return ws


@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="PR Chips Org", credit_balance=100.0)
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
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
                db.execute(delete(ProjectMemory).where(ProjectMemory.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _project(oid, *, with_request=False, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "running")
    kw.setdefault("ssh_private_key_enc", encrypt("PRIVATE-KEY-BODY"))
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.flush()
        db.add(ProjectRepo(project_id=p.id, ssh_uri=GL_REMOTE, role="primary",
                           provider="gitlab", is_push_target=True, auto_merge=True))
        db.add(ProjectMemory(project_id=p.id, author="customer", key="GITLAB_TOKEN",
                             value_enc=encrypt("glpat_x"), is_secret=True))
        rid = None
        if with_request:
            req = Request(project_id=p.id, type="feature", handling="ai",
                          status="in_progress", title="Add thing")
            db.add(req)
            db.flush()
            p.dev_request_id = req.id
            rid = req.id
        db.commit()
        return p.id, rid


# ---------------------------------------------------------------- pure helpers

def test_pr_ref_validation():
    assert tasks._pr_ref(7, "https://github.com/a/b/pull/7", "github") == \
        {"number": 7, "url": "https://github.com/a/b/pull/7", "provider": "github"}
    assert tasks._pr_ref(5, "https://gl/mr/5", "platform_gitlab")["provider"] == "gitlab"
    assert tasks._pr_ref(None, "https://gl/mr/5", "gitlab") is None
    assert tasks._pr_ref(5, None, "gitlab") is None
    assert tasks._pr_ref(5, "javascript:alert(1)", "gitlab") is None
    assert tasks._pr_meta(None) is None
    assert tasks._pr_meta({"number": 1, "url": "https://x", "provider": "github"}) == \
        {"prs": [{"number": 1, "url": "https://x", "provider": "github"}]}


def test_record_request_pr_appends_and_dedups(org):
    pid, rid = _project(org, with_request=True)
    r1 = {"number": 1, "url": "https://gh/pull/1", "provider": "github"}
    r2 = {"number": 2, "url": "https://gh/pull/2", "provider": "github"}
    with SyncSession() as db:
        req = db.get(Request, rid)
        tasks._record_request_pr(db, req, r1)
        tasks._record_request_pr(db, req, r1)  # dedup by url
        tasks._record_request_pr(db, req, r2)
        tasks._record_request_pr(db, req, None)  # no-op
        tasks._record_request_pr(db, None, r1)  # no-op
        db.commit()
    with SyncSession() as db:
        req = db.get(Request, rid)
        assert req.pr_urls == [r1, r2]  # oldest first, no duplicate
        assert request_out(req)["pr_urls"] == [r1, r2]


def test_request_out_defaults_empty(org):
    pid, rid = _project(org, with_request=True)
    with SyncSession() as db:
        assert request_out(db.get(Request, rid))["pr_urls"] == []


# ---------------------------------------------------------------- worker flows

def test_auto_merge_records_pr_and_chips_messages(org, monkeypatch, quiet):
    pid, rid = _project(org, with_request=True, dev_pr_number=5)
    monkeypatch.setattr(tasks.gitlab, "customer_mr_diff", lambda b, t, p, n: "diff")
    monkeypatch.setattr(tasks.gitlab, "customer_merge_mr",
                        lambda b, t, p, n, squash=True: (True, None))
    monkeypatch.setattr(tasks.pipeline, "run_security_review",
                        lambda db, p, diff: {"verdict": "pass", "findings": [], "floor": []})
    with SyncSession() as db:
        p = db.get(Project, pid)
        req = db.get(Request, rid)
        ops = tasks._remote_ops(GL_TARGET, "glpat_x")
        change = {"number": 5, "url": "https://gitlab.com/acme/widgets/-/merge_requests/5"}
        tasks._record_request_pr(db, req, tasks._pr_ref(5, change["url"], "gitlab"))
        tasks._remote_auto_merge(db, p, GL_TARGET, ops, change, f"request:{rid}", req, "logs")
    with SyncSession() as db:
        req = db.get(Request, rid)
        assert req.pr_urls == [{"number": 5, "url": "https://gitlab.com/acme/widgets/-/merge_requests/5",
                                "provider": "gitlab"}]
        merged_msg = (db.query(Message)
                      .filter_by(project_id=pid, thread=f"request:{rid}", author="agent")
                      .order_by(Message.created_at.desc()).first())
        assert "was merged" in merged_msg.body
        assert merged_msg.meta == {"prs": req.pr_urls}


def test_sweep_merged_message_carries_pr_meta(org, monkeypatch, quiet):
    pid, rid = _project(org, with_request=True, dev_run_state="awaiting_merge",
                        status="awaiting_customer", dev_pr_number=5,
                        dev_pr_url="https://gitlab.com/acme/widgets/-/merge_requests/5")
    monkeypatch.setattr(tasks.gitlab, "customer_get_mr",
                        lambda b, t, p, iid: {"state": "merged"})
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "deploying"
        msg = (db.query(Message).filter_by(project_id=pid, thread=f"request:{rid}")
               .order_by(Message.created_at.desc()).first())
        assert "was merged" in msg.body
        assert msg.meta == {"prs": [{"number": 5,
                                     "url": "https://gitlab.com/acme/widgets/-/merge_requests/5",
                                     "provider": "gitlab"}]}
