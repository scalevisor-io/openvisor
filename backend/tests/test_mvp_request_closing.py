"""§threads Request #0: a terminal project closes its initial-build request.

Closing it used to live in `approve_delivery` alone, so every OTHER route to a
terminal status - the admin status route, the hub - left the project closed with
its Request #0 still `in_progress`. On a project with no MVP phase that was
permanent: `validate_request` refuses mvp rows, `approve_delivery` needs a demo
that will never be deployed, and nothing else touched it. Owning this in
`lifecycle` is what makes every route agree.

The two endings mean different things and the request must not lie about which:
finished = a delivery the customer accepted, canceled = nothing was delivered.
"""
import pytest

from app.core.db import SyncSession
from app.models import Organization, Project, Request
from app.services.lifecycle import transition_sync


def _project_with_mvp(db, status="development", **kw):
    org = Organization(name="Mvp Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    project = Project(org_id=org.id, name="P", description="d", kind="ai",
                      status=status, **kw)
    db.add(project)
    db.flush()
    req = Request(project_id=project.id, type="mvp", handling="ai",
                  status="in_progress", title="Initial build")
    db.add(req)
    db.flush()
    return project, req


def test_finishing_a_project_closes_its_initial_build_as_done(monkeypatch):
    with SyncSession() as db:
        try:
            project, req = _project_with_mvp(db, status="awaiting_customer")
            transition_sync(db, project, "finished", "admin", "Closed by the admin")
            db.flush()
            assert req.status == "done"
        finally:
            db.rollback()


def test_canceling_a_project_rejects_its_initial_build(monkeypatch):
    """Not `done` - a canceled project delivered nothing, and the request is the
    customer's record of that."""
    with SyncSession() as db:
        try:
            project, req = _project_with_mvp(db, status="development")
            transition_sync(db, project, "canceled", "admin", "Abandoned")
            db.flush()
            assert req.status == "rejected"
        finally:
            db.rollback()


def test_a_non_terminal_transition_leaves_the_request_alone():
    with SyncSession() as db:
        try:
            project, req = _project_with_mvp(db, status="development")
            transition_sync(db, project, "awaiting_customer", "admin", None)
            db.flush()
            assert req.status == "in_progress"
        finally:
            db.rollback()


def test_an_already_closed_request_is_not_reopened_or_relabelled():
    """A delivery accepted earlier stays `done` even if the project is later
    canceled - history is not rewritten by a subsequent transition."""
    with SyncSession() as db:
        try:
            project, req = _project_with_mvp(db, status="awaiting_customer")
            transition_sync(db, project, "finished", "admin", None)
            db.flush()
            assert req.status == "done"
            transition_sync(db, project, "development", "admin", None)
            transition_sync(db, project, "canceled", "admin", None)
            db.flush()
            assert req.status == "done"
        finally:
            db.rollback()


def test_other_open_requests_are_untouched():
    """Only Request #0 is closed: a feature request has its own lifecycle, and
    marking it done would claim work that was never delivered."""
    with SyncSession() as db:
        try:
            project, mvp = _project_with_mvp(db, status="development")
            feature = Request(project_id=project.id, type="feature", handling="ai",
                              status="in_progress", title="Add export")
            db.add(feature)
            db.flush()
            transition_sync(db, project, "canceled", "admin", None)
            db.flush()
            assert mvp.status == "rejected"
            assert feature.status == "in_progress"
        finally:
            db.rollback()


def test_a_project_without_an_mvp_request_transitions_fine():
    """auto_dev and chat projects are never given a Request #0."""
    with SyncSession() as db:
        try:
            org = Organization(name="Mvp Org", credit_balance=10.0)
            db.add(org)
            db.flush()
            project = Project(org_id=org.id, name="P", description="d",
                              kind="auto_dev", status="development")
            db.add(project)
            db.flush()
            transition_sync(db, project, "canceled", "admin", None)
            db.flush()
            assert project.status == "canceled"
        finally:
            db.rollback()
