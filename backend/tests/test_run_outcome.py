"""§run outcome: the agent declares how the session ended; the platform
cross-checks the declaration against what actually reached the branch.

The pipeline's own evidence at the no-changes gate is one bit - "no publishable
diff" - but that bit means three different things, and prod produced all three
in one week: an investigation that correctly found nothing (should succeed), an
agent that believed it delivered while its files sat untracked (should fail,
PRECISELY - the run that declared a gitignored runbook "delivered"), and a run
that was genuinely blocked (should fail with the blocker, not generic copy).
outcome.json is the agent's declaration; the matrix here decides the verdict.
A missing or malformed declaration degrades to the pre-contract behavior, so a
non-compliant agent changes nothing.
"""
import json

import pytest

from app.core.db import SyncSession
from app.models import Message, Organization, Project, Request
from app.workers import tasks


@pytest.fixture
def ws(tmp_path, monkeypatch):
    openvisor = tmp_path / ".openvisor"
    openvisor.mkdir()
    monkeypatch.setattr(tasks.dev_concurrency, "run_ws", lambda p, run=None: tmp_path)
    return openvisor


def _project(db, **kw):
    org = Organization(name="Outcome Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai",
                status="development", **kw)
    db.add(p)
    db.flush()
    return p


def _request(db, project, type="feature"):
    req = Request(project_id=project.id, type=type, handling="ai",
                  status="in_progress", title="Check the thing")
    db.add(req)
    db.flush()
    project.dev_request_id = req.id
    return req


def _stub(monkeypatch, saved):
    monkeypatch.setattr(tasks, "_save_run",
                        lambda project, state, **kw: saved.update(state=state, **kw))
    monkeypatch.setattr(tasks, "_safe_transition",
                        lambda db, p, status, reason=None: saved.update(status=status))


def _declare(ws, outcome, summary=""):
    (ws / "outcome.json").write_text(json.dumps({"outcome": outcome,
                                                 "summary": summary}))


# ------------------------------------------------------------------ the matrix

def test_declared_no_change_closes_as_an_investigation(ws, monkeypatch):
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            req = _request(db, project)
            _declare(ws, "no_change_needed", "Prices all match; nothing to update.")

            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "done"
            assert req.status == "done"
            posted = db.query(Message).filter_by(project_id=project.id).all()
            assert any("nothing to update" in m.body for m in posted)
        finally:
            db.rollback()


def test_a_claimed_change_with_no_diff_fails_with_the_discrepancy_named(ws, monkeypatch):
    """The prod case: the agent declared a deliberately-untracked runbook
    'delivered'. The verdict must say committed-nothing, not generic no-changes
    copy - and must NOT close as an investigation even if a report also exists."""
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            req = _request(db, project)
            _declare(ws, "changed", "Added the canary runbook (untracked by design).")
            (ws / "report.md").write_text("I checked everything, all good.")

            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "failed"
            assert "nothing publishable" in saved["error"]
            assert req.status == "in_progress"          # resumable, not closed
        finally:
            db.rollback()


def test_a_declared_blocker_becomes_the_run_error(ws, monkeypatch):
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            _request(db, project)
            _declare(ws, "blocked", "The API token lacks write scope")

            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "failed"
            assert saved["error"] == "Blocked: The API token lacks write scope"
        finally:
            db.rollback()


def test_an_mvp_build_cannot_declare_no_change(ws, monkeypatch):
    """Request #0 is a build: producing nothing is a failure whatever it says."""
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            _request(db, project, type="mvp")
            _declare(ws, "no_change_needed", "All fine.")

            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "failed"
        finally:
            db.rollback()


def test_no_declaration_keeps_the_pre_contract_behavior(ws, monkeypatch):
    """report.md alone still closes an investigation (v13 agents), and nothing
    at all still fails with the generic copy - the contract only adds."""
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            req = _request(db, project)
            (ws / "report.md").write_text(
                "Checked the endpoints; every sample passed, no drift found.")
            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "done" and req.status == "done"
        finally:
            db.rollback()
    (ws / "report.md").unlink()          # the second half starts with nothing
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            req = _request(db, project)
            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "failed"
            assert "no changes to publish" in saved["error"]
        finally:
            db.rollback()


def test_a_malformed_declaration_is_ignored(ws, monkeypatch):
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            _request(db, project)
            (ws / "outcome.json").write_text('{"outcome": "victory!!", "summary": 1}')
            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "failed"           # degraded, not crashed
        finally:
            db.rollback()
