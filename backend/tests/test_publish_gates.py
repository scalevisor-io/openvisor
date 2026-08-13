"""Publish-quality gates: the §publish-gate sentinel predicate, the agent-authored
PR description reader (secret-redacted, template fallback), and the resume
steering-note query - the trio added after a capped run shipped a plumbing-only
commit as a PR and an "@agent …" chat note never reached the resumed run.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.db import SyncSession
from app.models import Message, Organization, Project, Request, utcnow
from app.services import leakscan
from app.workers import tasks


def test_no_changes_requires_exit_5_and_sentinel():
    logs = "runner: NO_CHANGES_TO_PUBLISH - the agent session produced no changes"
    assert tasks._no_changes({"exit_code": 5, "logs": logs})
    assert tasks._no_changes({"exit_code": "5", "logs": logs})
    # sentinel without the exit code (deeper failure owns the run) - not ours
    assert not tasks._no_changes({"exit_code": 1, "logs": logs})
    # exit code without the sentinel - not ours either
    assert not tasks._no_changes({"exit_code": 5, "logs": "runner: something else"})
    assert not tasks._no_changes(None)


@pytest.fixture
def ws_project(tmp_path):
    """A workspace-path-only stand-in: _agent_pr_body touches nothing else."""
    (tmp_path / ".openvisor").mkdir()
    return SimpleNamespace(workspace_path=str(tmp_path))


def _write_pr(ws_project, text):
    import pathlib
    pathlib.Path(ws_project.workspace_path, ".openvisor", "pr.md").write_text(text)


def test_agent_pr_body_missing_or_empty_falls_back(ws_project):
    assert tasks._agent_pr_body(None, ws_project) is None
    _write_pr(ws_project, "   \n  ")
    assert tasks._agent_pr_body(None, ws_project) is None


def test_agent_pr_body_redacts_secrets_and_blocks_pem(ws_project, monkeypatch):
    monkeypatch.setattr(leakscan, "platform_secret_values",
                        lambda *a, **k: ["sk-super-secret-value"])
    _write_pr(ws_project, "Bumped Tracelib to v4.\n\nToken used: sk-super-secret-value")
    body = tasks._agent_pr_body(None, ws_project)
    assert "sk-super-secret-value" not in body
    assert "Bumped Tracelib to v4." in body

    _write_pr(ws_project, "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    assert tasks._agent_pr_body(None, ws_project) is None


@pytest.fixture
def steering_project():
    with SyncSession() as db:
        org = Organization(name="Steering Test Org", credit_balance=1.0)
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai",
                    status="development")
        db.add(p)
        db.flush()
        t0 = utcnow() - timedelta(minutes=60)

        def msg(author, body, minutes, thread="main"):
            m = Message(project_id=p.id, thread=thread, author=author, body=body)
            db.add(m)
            db.flush()
            m.created_at = t0 + timedelta(minutes=minutes)
            db.flush()
            return m

        yield db, p, t0, msg
        db.rollback()


def test_steering_transcript_folds_conversation_since_dispatch(steering_project):
    db, p, t0, msg = steering_project
    since = t0 + timedelta(minutes=10)
    # nothing said -> nothing to steer with
    assert tasks._steering_note(db, p, since) is None
    # before the previous dispatch: the run already saw (or consumed) these
    msg("customer", "old instruction the last run already handled", 5)
    assert tasks._steering_note(db, p, since) is None
    # the window: notes left during and after the failed run, agent/system
    # narration excluded, chronological order, authors labeled
    msg("customer", "use SQLite instead of Postgres", 15)
    msg("agent", "Build failed: could not connect to Postgres", 20)
    msg("system", "Status -> awaiting_customer", 21)
    msg("admin", "@agent also drop the docker healthcheck", 25)
    assert tasks._steering_note(db, p, since) == (
        "[customer] use SQLite instead of Postgres\n\n"
        "[consultant] @agent also drop the docker healthcheck")


def test_steering_transcript_reads_request_thread_for_scoped_runs(steering_project):
    db, p, t0, msg = steering_project
    req = Request(project_id=p.id, type="feature", title="Add exports")
    db.add(req)
    db.flush()
    p.dev_request_id = req.id
    db.flush()
    since = t0 + timedelta(minutes=10)
    # a reply left in the request's own thread reaches the resumed run,
    # merged chronologically with main-thread notes; other threads stay out
    msg("customer", "the CSV export should use semicolons", 15,
        thread=f"request:{req.id}")
    msg("customer", "and please keep the header row", 16)
    msg("customer", "unrelated thread noise", 17, thread="request:other")
    assert tasks._steering_note(db, p, since) == (
        "[customer] the CSV export should use semicolons\n\n"
        "[customer] and please keep the header row")


def test_steering_transcript_fallback_without_dispatch_clock(steering_project):
    db, p, t0, msg = steering_project
    # no clock (defensive path): per-thread "after the agent last spoke"
    msg("agent", "PR #64 is ready", 0)
    assert tasks._steering_note(db, p, None) is None
    msg("admin", "@agent PR description is not satisfying. Update it.", 1)
    assert tasks._steering_note(db, p, None) == (
        "[consultant] @agent PR description is not satisfying. Update it.")
    # once the agent answers, the note is consumed
    msg("agent", "On it", 2)
    assert tasks._steering_note(db, p, None) is None


def test_steering_transcript_caps_keep_the_freshest(steering_project):
    db, p, t0, msg = steering_project
    since = t0 + timedelta(minutes=1)
    for i in range(tasks.STEERING_MAX_MESSAGES + 3):
        msg("customer", f"note {i}", 5 + i)
    text = tasks._steering_note(db, p, since)
    assert text.count("[customer]") == tasks.STEERING_MAX_MESSAGES
    assert "note 0" not in text and "note 12" in text
    # char budget: huge messages are truncated per message and the total capped
    msg("customer", "x" * 5000, 40)
    msg("customer", "final word", 41)
    text = tasks._steering_note(db, p, since)
    assert len(text) <= tasks.STEERING_MAX_CHARS + 100
    assert text.endswith("final word")


def test_prepare_runner_inputs_binds_dispatch_call_shape():
    # Regression: steering_note was once inserted as the 4th POSITIONAL param,
    # so _dispatch_runner's positional provider landed on it and every dev
    # dispatch died with "multiple values for argument 'steering_note'".
    # Bind the exact call shape _dispatch_runner uses - a bad reorder fails here.
    import inspect
    sig = inspect.signature(tasks._prepare_runner_inputs)
    bound = sig.bind("db", "project", fix_instruction=None, provider="github",
                     plan_only=False, approved_plan=None, steering_note="note")
    assert bound.arguments["provider"] == "github"
    assert bound.arguments["steering_note"] == "note"


def test_per_project_iteration_cap_resolution():
    # Dispatch resolves the effective cap: project override wins, null inherits
    # the instance default (DEV_MAX_ITERATIONS_DEFAULT, legacy DEV_MAX_ITERATIONS).
    from app.core.config import settings
    from app.schemas.schemas import ProjectPatchIn
    import pytest as _pytest
    from pydantic import ValidationError

    project = SimpleNamespace(dev_max_iterations=None)
    assert (project.dev_max_iterations or settings.dev_max_iterations_default) == \
        settings.dev_max_iterations_default
    project.dev_max_iterations = 120
    assert (project.dev_max_iterations or settings.dev_max_iterations_default) == 120

    assert ProjectPatchIn(dev_max_iterations=1).dev_max_iterations == 1
    assert ProjectPatchIn(dev_max_iterations=None).dev_max_iterations is None
    with _pytest.raises(ValidationError):
        ProjectPatchIn(dev_max_iterations=0)
    with _pytest.raises(ValidationError):
        ProjectPatchIn(dev_max_iterations=501)
