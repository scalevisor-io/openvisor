"""§investigation runs: a dev session whose honest outcome is "nothing to change".

"Check whether OVHCloud's pricing drifted from ours, and open an MR if it did"
is a complete task when nothing drifted - but the pipeline is built around
build → push → PR, so a run that committed nothing was recorded `failed` with
"The run produced no changes to publish" and the customer was told to describe
what they expected and hit Resume. For a question the agent had just answered
correctly. (Seen in production on request e3d26794 via a routine.)

The agent declares which case it is by writing `.openvisor/report.md`, so the
distinction rests on evidence from the run rather than on guessing intent up
front. These pin both directions: a report means done, no report still means
failed, and an MVP build can never talk its way out of an empty result.
"""
import pytest

from app.core.db import SyncSession
from app.models import DevRun, Message, Organization, Project, Request
from app.workers import tasks


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Point run_ws at a temp workspace so a test can drop artifacts in it."""
    openvisor = tmp_path / ".openvisor"
    openvisor.mkdir()
    monkeypatch.setattr(tasks.dev_concurrency, "run_ws", lambda p, run=None: tmp_path)
    return openvisor


def _project(db, **kw):
    org = Organization(name="Inv Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai",
                status="development", **kw)
    db.add(p)
    db.flush()
    return p


def _request(db, project, type="feature"):
    req = Request(project_id=project.id, type=type, handling="ai",
                  status="in_progress", title="Check the pricing")
    db.add(req)
    db.flush()
    project.dev_request_id = req.id
    return req


def _stub(monkeypatch, saved):
    monkeypatch.setattr(tasks, "_save_run",
                        lambda project, state, **kw: saved.update(state=state, **kw))
    monkeypatch.setattr(tasks, "_safe_transition",
                        lambda db, p, status, reason=None: saved.update(status=status))


# ---------------------------------------------------------------- the answer

def test_a_report_closes_the_run_as_done_and_answers_the_request(ws, monkeypatch):
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            req = _request(db, project)
            (ws / "report.md").write_text(
                "Checked OVHCloud's published rates against our price table: all six "
                "models match, no drift. No change warranted.")

            tasks._fail_no_changes(db, project, "logs")
            db.flush()

            assert saved["state"] == "done"          # not a failed build
            assert req.status == "done"              # the answer IS the delivery
            posted = db.query(Message).filter_by(project_id=project.id).all()
            assert any("no drift" in m.body for m in posted)
        finally:
            db.rollback()


def test_without_a_report_an_empty_run_still_fails(ws, monkeypatch):
    """The old behaviour has to survive: a build that produced nothing and says
    nothing about why is a failure, and must keep telling the customer so."""
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
            assert req.status == "in_progress"       # still open, resumable
        finally:
            db.rollback()


def test_an_mvp_build_cannot_report_its_way_out_of_an_empty_result(ws, monkeypatch):
    """Request #0 is a build, not an investigation: producing nothing is a
    failure however eloquently the run explains itself."""
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            _request(db, project, type="mvp")
            (ws / "report.md").write_text("Nothing needed doing, honest.")

            tasks._fail_no_changes(db, project, "logs")
            db.flush()

            assert saved["state"] == "failed"
        finally:
            db.rollback()


def test_a_run_with_no_bound_request_still_fails(ws, monkeypatch):
    """No request means an MVP-era build; the report path needs a scoped ask."""
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            (ws / "report.md").write_text("All good.")

            tasks._fail_no_changes(db, project, "logs")
            db.flush()

            assert saved["state"] == "failed"
        finally:
            db.rollback()


# ---------------------------------------------------------------- the filter

def test_key_material_in_a_report_drops_it_wholesale(ws, monkeypatch):
    """The report goes straight into the customer's thread, so it gets the same
    defensive pass as the PR description - and dropping it falls back to the
    failure path rather than posting anything."""
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            _request(db, project)
            (ws / "report.md").write_text(
                "Findings:\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\n"
                "-----END RSA PRIVATE KEY-----\n")

            assert tasks._agent_report(project) is None
            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "failed"
        finally:
            db.rollback()


def test_an_empty_report_file_is_not_an_answer(ws, monkeypatch):
    with SyncSession() as db:
        try:
            saved = {}
            _stub(monkeypatch, saved)
            project = _project(db)
            _request(db, project)
            (ws / "report.md").write_text("   \n  ")

            assert tasks._agent_report(project) is None
            tasks._fail_no_changes(db, project, "logs")
            db.flush()
            assert saved["state"] == "failed"
        finally:
            db.rollback()
