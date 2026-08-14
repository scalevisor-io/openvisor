"""§steering scope: which conversations may steer a run.

The steering transcript was project-global - every customer/consultant message
in MAIN plus the run's thread. Built for the serialized world where main only
ever discussed THE build, it broke the morning routines and parallel builds
met chat: a main-thread ask ("add our LinkedIn link to the landing") was
correctly classified into its own request AND folded into two unrelated
pricing dispatches as "newer customer guidance" - both agents obeyed the
steering over their task, and two pricing-titled MRs shipped footer code.

The rule now: a scoped request run listens to its OWN thread only; the
MVP/unscoped build keeps main, because there main IS the build conversation.
And both the thread resolution and the steering scope key on the BOUND run,
not the Project.dev_request_id mirror a parallel sibling can restamp.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.db import SyncSession
from app.models import DevRun, Message, Organization, Project, Request, utcnow
from app.workers import tasks


def _project(db, **kw):
    org = Organization(name="Steer Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai",
                status="development", **kw)
    db.add(p)
    db.flush()
    return p


def _request(db, project, type="feature", title="Check pricing"):
    req = Request(project_id=project.id, type=type, handling="ai",
                  status="in_progress", title=title)
    db.add(req)
    db.flush()
    return req


def _msg(db, project, thread, body, author="admin", minutes_ago=5):
    m = Message(project_id=project.id, thread=thread, author=author, body=body,
                created_at=utcnow() - timedelta(minutes=minutes_ago))
    db.add(m)
    db.flush()
    return m


def _bind(project, req):
    project._dev_run = SimpleNamespace(request_id=req.id if req else None,
                                       workspace_dir="", branch=None,
                                       predecessor_id=None)


# --------------------------------------------------------------- the incident

def test_a_scoped_run_never_steers_from_the_main_thread():
    """The prod shape verbatim: a main-thread ask about OTHER work must not
    reach a scoped run's steering."""
    with SyncSession() as db:
        try:
            project = _project(db)
            req = _request(db, project)
            _bind(project, req)
            since = utcnow() - timedelta(minutes=60)
            _msg(db, project, "main",
                 "Add our social link https://linkedin.example/x to our landing")
            note = tasks._steering_note(db, project, since)
            assert note is None
        finally:
            db.rollback()


def test_a_scoped_run_still_steers_from_its_own_thread():
    with SyncSession() as db:
        try:
            project = _project(db)
            req = _request(db, project)
            _bind(project, req)
            since = utcnow() - timedelta(minutes=60)
            _msg(db, project, f"request:{req.id}", "Focus on the embeddings rows")
            _msg(db, project, "main", "Unrelated main chatter")
            note = tasks._steering_note(db, project, since)
            assert note is not None
            assert "Focus on the embeddings rows" in note
            assert "Unrelated main chatter" not in note
        finally:
            db.rollback()


def test_the_mvp_build_keeps_steering_from_main():
    """For the initial build, main IS the conversation - '@agent do X' typed
    there before a Resume must keep reaching the run."""
    with SyncSession() as db:
        try:
            project = _project(db)
            mvp = _request(db, project, type="mvp", title="Initial build")
            _bind(project, mvp)
            since = utcnow() - timedelta(minutes=60)
            _msg(db, project, "main", "@agent use PostgreSQL, not SQLite")
            note = tasks._steering_note(db, project, since)
            assert note is not None and "PostgreSQL" in note
        finally:
            db.rollback()


# --------------------------------------------------------------- the mirror

def test_dev_thread_prefers_the_bound_run_over_the_mirror():
    """A parallel sibling restamping the mirror mid-dispatch must not redirect
    this run's narration - or its steering - into the wrong thread."""
    with SyncSession() as db:
        try:
            project = _project(db)
            mine = _request(db, project, title="Mine")
            other = _request(db, project, title="Someone else's")
            project.dev_request_id = other.id      # the sibling stamped last
            _bind(project, mine)
            assert tasks._dev_thread(db, project) == f"request:{mine.id}"
        finally:
            db.rollback()


def test_steering_scope_follows_the_bound_run_not_the_mirror():
    with SyncSession() as db:
        try:
            project = _project(db)
            mine = _request(db, project, title="Mine")
            other = _request(db, project, title="Someone else's")
            project.dev_request_id = other.id
            _bind(project, mine)
            since = utcnow() - timedelta(minutes=60)
            _msg(db, project, f"request:{other.id}", "steer the OTHER build")
            _msg(db, project, f"request:{mine.id}", "steer MY build")
            note = tasks._steering_note(db, project, since)
            assert note is not None
            assert "steer MY build" in note
            assert "steer the OTHER build" not in note
        finally:
            db.rollback()


def test_unbound_legacy_resolution_still_uses_the_mirror():
    with SyncSession() as db:
        try:
            project = _project(db)
            req = _request(db, project)
            project.dev_request_id = req.id
            project._dev_run = None
            assert tasks._dev_thread(db, project) == f"request:{req.id}"
        finally:
            db.rollback()
