"""§threads live threads: a customer/consultant reply inside a request's own
thread classifies with a reduced, request-scoped action set - confirm a
proposed request, or resume the parked run the thread belongs to (the scoped
request in flight, or Request #0 for a parked MVP build). Everything else
stays silent. test_build_control style: committed throwaway org, tasks open
their own sessions.
"""
import pytest
from sqlalchemy import delete, select, update

from app.core.db import SyncSession
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, Request,
    StatusChange,
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
def org_id():
    with SyncSession() as db:
        org = Organization(name="ThreadClassifier Test Org", credit_balance=100.0)
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
                # dev_request_id references request rows - clear it before deleting them
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


def _commit_project(oid, tmp_path, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "done")
    kw.setdefault("workspace_path", str(tmp_path))
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.commit()
        return p.id


def _request(pid, **kw):
    kw.setdefault("type", "feature")
    kw.setdefault("handling", "ai")
    kw.setdefault("status", "proposed")
    kw.setdefault("title", "CSV export")
    with SyncSession() as db:
        req = Request(project_id=pid, **kw)
        db.add(req)
        db.commit()
        return req.id


def _thread_msg(pid, rid, body="go ahead", author="customer"):
    with SyncSession() as db:
        msg = Message(project_id=pid, thread=f"request:{rid}", author=author,
                      body=body)
        db.add(msg)
        db.commit()
        return msg.id


def _classify_with(monkeypatch, verdict):
    calls: list = []

    def fake(db, project, context, body, **kw):
        calls.append(context)
        return verdict

    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent", fake)
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, project: ("", "k", "m"))
    return calls


def test_thread_confirm_starts_its_own_request(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, gitlab_project_id=1,
                          demo_deployed_once=True)
    rid = _request(pid)
    mid = _thread_msg(pid, rid, "yes, go ahead")
    calls = _classify_with(monkeypatch, {"intent": "confirm", "request_type": None,
                                         "summary": None})
    dispatched: list = []
    monkeypatch.setattr(tasks.handle_request, "apply_async",
                        lambda args=None, **k: dispatched.append(args))

    tasks.classify_chat_message(pid, mid)

    assert len(calls) == 1 and "CSV export" in calls[0]
    with SyncSession() as db:
        assert db.get(Request, rid).status == "open"
        ack = db.execute(select(Message.body).where(
            Message.project_id == pid, Message.author == "agent",
            Message.thread == f"request:{rid}")).scalars().first()
        assert ack.startswith('On it - starting "CSV export"')
    assert dispatched == [[pid, rid, mid]]


def test_thread_resume_retries_the_parked_scoped_run(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="failed",
                          status="awaiting_customer", gitlab_project_id=1)
    rid = _request(pid, status="in_progress")
    with SyncSession() as db:
        db.get(Project, pid).dev_request_id = rid
        db.commit()
    mid = _thread_msg(pid, rid, "fixed the token, try again")
    _classify_with(monkeypatch, {"intent": "resume", "request_type": None,
                                 "summary": None})
    dispatched: list = []
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda args=None, kwargs=None, **k: dispatched.append((args, kwargs)))

    tasks.classify_chat_message(pid, mid)

    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.status == "development"
        ack = db.execute(select(Message.body).where(
            Message.project_id == pid, Message.author == "agent",
            Message.thread == f"request:{rid}")).scalars().first()
        assert "retrying the build" in ack
    assert len(dispatched) == 1 and dispatched[0][0] == [pid] and dispatched[0][1]["fix_only"] is True and dispatched[0][1]["run_id"]


def test_mvp_thread_resume_covers_the_parked_mvp_run(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="failed",
                          status="awaiting_customer", gitlab_project_id=1)
    rid = _request(pid, type="mvp", status="in_progress", title="Initial build")
    mid = _thread_msg(pid, rid, "please retry with SQLite")
    _classify_with(monkeypatch, {"intent": "resume", "request_type": None,
                                 "summary": None})
    dispatched: list = []
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda args=None, kwargs=None, **k: dispatched.append((args, kwargs)))

    tasks.classify_chat_message(pid, mid)

    assert len(dispatched) == 1 and dispatched[0][0] == [pid] and dispatched[0][1]["fix_only"] is True and dispatched[0][1]["run_id"]


def test_thread_branch_ignores_out_of_scope_verdicts_and_threads(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, gitlab_project_id=1,
                          demo_deployed_once=True)
    rid = _request(pid)
    # a new_request verdict inside a thread does nothing - requests are filed
    # from main
    mid = _thread_msg(pid, rid, "also add PDF export")
    _classify_with(monkeypatch, {"intent": "new_request", "request_type": "feature",
                                 "summary": "PDF export"})
    tasks.classify_chat_message(pid, mid)
    with SyncSession() as db:
        assert db.get(Request, rid).status == "proposed"
        assert db.execute(select(Message).where(
            Message.project_id == pid, Message.author == "agent")).scalars().first() is None

    # A request with nothing to ACT on (done, no parked run) still reaches the
    # classifier - §work answers made that thread answerable, and it is where
    # "what did you do?" gets asked - but an action verdict there changes nothing.
    done_rid = _request(pid, status="done", title="Old work")
    mid2 = _thread_msg(pid, done_rid, "thanks!")
    calls = _classify_with(monkeypatch, {"intent": "confirm", "request_type": None,
                                         "summary": None})
    monkeypatch.setattr(tasks.answer_work_question, "apply_async",
                        lambda args=None, **k: None)
    tasks.classify_chat_message(pid, mid2)
    assert len(calls) == 1
    with SyncSession() as db:
        assert db.get(Request, done_rid).status == "done"
        assert db.execute(select(Message).where(
            Message.project_id == pid, Message.author == "agent",
            Message.thread == f"request:{done_rid}")).scalars().first() is None
