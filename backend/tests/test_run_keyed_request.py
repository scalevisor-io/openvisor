"""§parallel-builds: the run pipeline keys the request it builds on the BOUND
run's row, never on the Project.dev_request_id mirror.

Prod, 2026-08-27, a project at limit 5: request A's Start fresh was dispatched
50 s after a sibling run on request B had taken the mirror. A's run read B off
the mirror at every step - it was handed B's task text, named B's branch,
opened B's merge request and recorded it on B, all under A's row. That sibling
finished first and CLEARED the mirror, so B's own resume found no request,
built the MVP task and opened a "chore/project-setup" MR titled "MVP build".
`_dev_thread` and the steering scope had already moved onto the row
(test_steering_scope); these pin the rest of the pipeline on
`dev_concurrency.run_request`.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.models import Message, Organization, Project, Request, StatusChange
from app.services import dev_concurrency
from app.workers import tasks


def _project(db, **kw):
    org = Organization(name="RunKeyed Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai",
                status="development", **kw)
    db.add(p)
    db.flush()
    return p


def _request(db, project, title, type="bug", body=None):
    req = Request(project_id=project.id, type=type, handling="ai",
                  status="in_progress", title=title)
    db.add(req)
    db.flush()
    if body:
        db.add(Message(project_id=project.id, thread=f"request:{req.id}",
                       author="customer", body=body))
        db.flush()
    return req


def _bind(project, req):
    project._dev_run = SimpleNamespace(
        request_id=req.id if req else None, workspace_dir="", branch=None,
        predecessor_id=None, repo_id=None, tokens_consumed=0, cost_credits=0.0,
        billed_through=0)


def _siblings(db):
    """The prod shape: A is the bound run, the mirror names its sibling B."""
    project = _project(db)
    a = _request(db, project, "Fix landing link color on click",
                 body="All landing links must keep their original colors when clicked")
    b = _request(db, project, "Fix card description truncation",
                 body="The blog card description is clipped to 140 characters")
    project.dev_request_id = b.id
    _bind(project, a)
    return project, a, b


def _quiet_close(monkeypatch, saved):
    monkeypatch.setattr(tasks, "_save_run",
                        lambda project, state, **kw: saved.update(state=state, **kw))
    monkeypatch.setattr(tasks, "_safe_transition",
                        lambda db, p, status, reason=None: saved.update(status=status))


# ------------------------------------------------------------- the resolver

def test_the_bound_row_wins_over_the_mirror():
    with SyncSession() as db:
        try:
            project, a, b = _siblings(db)
            assert dev_concurrency.run_request(db, project).id == a.id
            # an MVP row is an unscoped build: None, whatever the mirror says
            mvp = _request(db, project, "Initial build", type="mvp")
            _bind(project, mvp)
            assert dev_concurrency.run_request(db, project) is None
            # no bound row (a legacy single-checkout dispatch): the mirror stands
            project._dev_run = None
            assert dev_concurrency.run_request(db, project).id == b.id
            project.dev_request_id = None
            assert dev_concurrency.run_request(db, project) is None
        finally:
            db.rollback()


# ------------------------------------------------- what the sandbox is told

def test_the_task_file_scopes_the_bound_runs_request(monkeypatch):
    with SyncSession() as db:
        try:
            project, a, b = _siblings(db)
            seen = []
            monkeypatch.setattr(tasks, "_context_repos", lambda db, project: [])
            monkeypatch.setattr(tasks, "_effective_memory", lambda db, project: [])
            monkeypatch.setattr(tasks, "_project_files_meta", lambda db, project: [])
            monkeypatch.setattr(tasks.rag, "search", lambda *a, **k: [])
            monkeypatch.setattr(tasks.rag, "rules_digests", lambda *a, **k: [])
            monkeypatch.setattr(tasks.rag, "procedures_for",
                                lambda db, query, kb_ids: seen.append(query) or [])
            from app.services import speciality as spec
            monkeypatch.setattr(spec, "deliverable_clause", lambda p: "x")
            monkeypatch.setattr(spec, "knowledge_tags", lambda p: [])
            monkeypatch.setattr(spec, "one_shot_example", lambda p: "")
            from app.agents import pipeline as pl
            monkeypatch.setattr(pl, "_project_context", lambda db, p: "ctx")

            text, _ = tasks._build_task_file(db, project)

            assert f"### {a.title}" in text
            assert "original colors when clicked" in text
            assert b.title not in text
            assert "140 characters" not in text
            # the procedure registry is asked about THIS task, not the sibling's
            assert seen and seen[0].startswith(a.title)
        finally:
            db.rollback()


# ------------------------------------------------ what the run leaves behind

def test_the_work_summary_and_mr_copy_follow_the_bound_run(monkeypatch):
    with SyncSession() as db:
        try:
            project, a, b = _siblings(db)
            monkeypatch.setattr(tasks, "_agent_pr_body", lambda db, p: "What changed")
            title, _body = tasks._platform_mr_copy(db, project)
            assert a.title in title
            assert b.title not in title
            assert a.work_summary == "What changed"
            assert b.work_summary is None
        finally:
            db.rollback()


def test_dev_run_usage_bills_the_bound_runs_request(monkeypatch, tmp_path):
    with SyncSession() as db:
        try:
            project, a, b = _siblings(db)
            (tmp_path / ".openvisor").mkdir()
            (tmp_path / ".openvisor" / "usage.json").write_text(
                json.dumps({"input_tokens": 10, "output_tokens": 5}))
            monkeypatch.setattr(tasks.dev_concurrency, "run_ws",
                                lambda p, run=None: tmp_path)
            monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
            billed = []
            from app.services import llm
            monkeypatch.setattr(
                llm, "record_usage",
                lambda db, project, usage, label, request=None:
                    billed.append((label, request)) or 0.5)

            tasks._bill_dev_run(db, project)

            assert billed == [(f"dev run - {a.title}", a)]
            assert project._dev_run.tokens_consumed == 15
        finally:
            db.rollback()


def test_a_no_change_run_closes_the_bound_runs_request(monkeypatch, tmp_path):
    with SyncSession() as db:
        try:
            project, a, b = _siblings(db)
            (tmp_path / ".openvisor").mkdir()
            (tmp_path / ".openvisor" / "report.md").write_text(
                "Checked every landing link: the colors already hold on click.")
            monkeypatch.setattr(tasks.dev_concurrency, "run_ws",
                                lambda p, run=None: tmp_path)
            saved = {}
            _quiet_close(monkeypatch, saved)

            tasks._fail_no_changes(db, project, "logs")
            db.flush()

            assert saved["state"] == "done"
            assert a.status == "done"
            assert b.status == "in_progress"
            posted = db.query(Message).filter_by(project_id=project.id,
                                                 author="agent").all()
            assert posted and all(m.thread == f"request:{a.id}" for m in posted)
        finally:
            db.rollback()


@pytest.fixture
def org_cleanup():
    """_finalize_pr_deliverable commits: sweep what the test created."""
    ids = {}
    try:
        yield ids
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(
                Project.org_id == ids["org"])).scalars().all()
            if pids:
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                for pid in pids:
                    db.get(Project, pid).dev_request_id = None
                db.flush()
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
                db.execute(delete(Project).where(Project.id.in_(pids)))
            db.execute(delete(Organization).where(Organization.id == ids["org"]))
            db.commit()


def test_a_pr_deliverable_closes_its_own_request_and_keeps_the_siblings_mirror(
        monkeypatch, org_cleanup):
    with SyncSession() as db:
        project, a, b = _siblings(db)
        org_cleanup["org"] = project.org_id
        saved = {}
        _quiet_close(monkeypatch, saved)

        tasks._finalize_pr_deliverable(db, project, "Merged.", None)

        db.refresh(a)
        db.refresh(b)
        db.refresh(project)
        assert a.status == "done"
        assert b.status == "in_progress"
        # the mirror names the live sibling B: not this run's to clear
        assert project.dev_request_id == b.id
