"""§revise: "actually, make it X" while the pull request is open.

The gap this closes (hit in prod regression): with a run parked in
awaiting_merge, an admin wrote "Actually this is a Paid solution : fix the MR"
in the request's thread and NOTHING happened - the classifier logged intent
`none` because every action was gated on a free run slot and on a FAILED run to
resume, and neither holds while a PR waits for its merge. A change request on
open work now takes another pass on the same branch instead.
"""
import uuid

import pytest
from sqlalchemy import delete, select

from app.api.serializers import dev_resume_capability, dev_revise_capability
from app.core.db import SyncSession
from app.models import DevRun, Message, Organization, Project, ProjectRepo, Request, StatusChange
from app.services import dev_concurrency
from app.workers import tasks


@pytest.fixture
def project():
    with SyncSession() as db:
        org = Organization(name="Revise Org", credit_balance=100.0)
        db.add(org)
        db.commit()
        p = Project(org_id=org.id, name="R", description="d", kind="ai",
                    status="awaiting_customer", dev_run_state="awaiting_merge",
                    gitlab_project_id=42, workspace_path="/tmp/revise-test")
        db.add(p)
        db.commit()
        req = Request(project_id=p.id, title="Include dev.meta.ai",
                      type="feature", handling="ai", status="open")
        db.add(req)
        db.commit()
        p.dev_request_id = req.id
        run = DevRun(project_id=p.id, request_id=req.id, state="awaiting_merge",
                     branch="docs/add-dev-meta-ai", pr_number=9,
                     pr_url="https://github.com/o/r/pull/9")
        db.add(run)
        db.commit()
        ids = (p.id, req.id, run.id, org.id)
    yield ids
    with SyncSession() as db:
        pid, _, _, oid = ids
        proj = db.get(Project, pid)
        if proj is not None:
            proj.dev_request_id = None  # FK: the project points at the request
            db.commit()
        db.execute(delete(Message).where(Message.project_id == pid))
        db.execute(delete(StatusChange).where(StatusChange.project_id == pid))
        db.execute(delete(DevRun).where(DevRun.project_id == pid))
        db.execute(delete(ProjectRepo).where(ProjectRepo.project_id == pid))
        db.execute(delete(Request).where(Request.project_id == pid))
        db.execute(delete(Project).where(Project.id == pid))
        db.execute(delete(Organization).where(Organization.id == oid))
        db.commit()


# ---------------------------------------------------------------- capability

def test_revise_capability_is_the_awaiting_merge_twin_of_resume(project):
    pid, *_ = project
    with SyncSession() as db:
        p = db.get(Project, pid)
        # resume says no (nothing failed) - which is exactly why the ask went nowhere
        assert dev_resume_capability(p) == (False, "Waiting for you to merge the pull request")
        assert dev_revise_capability(p) == (True, None)

        p.dev_run_state = "running"
        assert dev_revise_capability(p)[0] is False
        p.dev_run_state = "awaiting_merge"
        p.block_auto_development = True
        assert dev_revise_capability(p)[0] is False
        p.block_auto_development = False
        p.status = "finished"
        assert dev_revise_capability(p)[0] is False


# ---------------------------------------------------------------- slot handover

def test_release_for_revision_hands_the_slot_and_the_branch_over(project):
    pid, rid, run_id, _ = project
    with SyncSession() as db:
        p, req = db.get(Project, pid), db.get(Request, rid)
        assert dev_concurrency.slots_full(db, p) is True  # today's refusal
        predecessor = dev_concurrency.release_for_revision(db, p, req)
        assert predecessor.id == run_id and predecessor.state == "superseded"
        db.commit()
        # the superseded row no longer holds the slot, so the revision is admitted
        # even though the project scalar still mirrors it (run_development flips
        # that to running when the worker picks the job up)
        assert p.dev_run_state == "awaiting_merge"
        new = dev_concurrency.acquire_slot(db, p, req, predecessor=predecessor)
        db.commit()
        assert new.id != run_id
        # same branch = the OPEN pull request collects the revision's commits
        assert new.branch == "docs/add-dev-meta-ai"
        # and billing starts from zero on the new row (the watermark is per row)
        assert (new.billed_through or 0) == 0


def test_dispatch_revision_carries_the_pr_pointer_and_queues_a_fix_run(project, monkeypatch):
    pid, rid, run_id, _ = project
    sent = {}
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **kw: sent.update(kw.get("kwargs") or {}))
    with SyncSession() as db:
        p, req = db.get(Project, pid), db.get(Request, rid)
        assert tasks._dispatch_revision(db, p, req) == "same"
    assert sent["fix_only"] is True
    with SyncSession() as db:
        rows = {r.id: r for r in db.execute(
            select(DevRun).where(DevRun.project_id == pid)).scalars().all()}
        assert rows[run_id].state == "superseded"
        new = next(r for r in rows.values() if r.id != run_id)
        assert new.id == sent["run_id"]
        # the merge sweep must still find its PR once this run parks again
        assert (new.pr_number, new.pr_url) == (9, "https://github.com/o/r/pull/9")


def test_dispatch_revision_is_a_no_op_without_an_open_pr(project, monkeypatch):
    pid, rid, run_id, _ = project
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **kw: pytest.fail("must not dispatch"))
    with SyncSession() as db:
        db.get(DevRun, run_id).state = "done"
        db.commit()
        p, req = db.get(Project, pid), db.get(Request, rid)
        assert tasks._dispatch_revision(db, p, req) is None


# ------------------------------------------------- closed PR = fresh work unit

GH_TARGET = {"provider": "github", "owner": "o", "repo": "r"}


def test_dispatch_revision_goes_fresh_when_the_pr_was_closed(project, monkeypatch):
    """Feedback after the customer closed the PR unmerged (before the sweep saw
    it): the pass still runs, but as a NEW work unit - no branch or PR pointer
    reaches the new run, so it publishes a fresh PR instead of the closed one."""
    pid, rid, run_id, _ = project
    sent = {}
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **kw: sent.update(kw.get("kwargs") or {}))
    monkeypatch.setattr(tasks, "_pr_closed_unmerged", lambda db, p, t, n: True)
    with SyncSession() as db:
        p, req = db.get(Project, pid), db.get(Request, rid)
        p.dev_branch = "docs/add-dev-meta-ai"
        p.dev_pr_number, p.dev_pr_url = 9, "https://github.com/o/r/pull/9"
        assert tasks._dispatch_revision(db, p, req) == "fresh"
    assert sent["fix_only"] is True
    with SyncSession() as db:
        new = db.get(DevRun, sent["run_id"])
        assert (new.branch, new.pr_number, new.pr_url) == (None, None, None)
        assert db.get(DevRun, run_id).state == "superseded"
        p = db.get(Project, pid)
        assert (p.dev_branch, p.dev_pr_number, p.dev_pr_url) == (None, None, None)


def test_reset_stale_branch_clears_the_bound_run_row_too(project, monkeypatch):
    """Regression (awesome-llm-devops#9): only the project scalars were cleared,
    the run row's branch copy survived, so the resume re-pushed the deleted
    branch and the agent reopened the closed PR it was still handed."""
    pid, _, run_id, _ = project
    monkeypatch.setattr(tasks, "_project_repo_token", lambda db, p, prov, uri=None: "tok")
    monkeypatch.setattr(tasks.github, "branch_exists",
                        lambda o, r, b, token=None: False)
    with SyncSession() as db:
        p = db.get(Project, pid)
        p.dev_branch = "docs/add-dev-meta-ai"
        p.dev_pr_number, p.dev_pr_url = 9, "u"
        row = db.get(DevRun, run_id)
        dev_concurrency.bind_run(p, row)
        tasks._reset_stale_branch(db, p, GH_TARGET)
        assert (p.dev_branch, p.dev_pr_number, p.dev_pr_url) == (None, None, None)
        assert (row.branch, row.pr_number, row.pr_url) == (None, None, None)


def test_reset_stale_branch_treats_a_closed_unmerged_pr_as_stale(project, monkeypatch):
    """The branch still exists but its PR was closed without merging: same
    verdict - rejected work, fresh unit."""
    pid, _, run_id, _ = project
    monkeypatch.setattr(tasks, "_project_repo_token", lambda db, p, prov, uri=None: "tok")
    monkeypatch.setattr(tasks.github, "branch_exists",
                        lambda o, r, b, token=None: True)
    monkeypatch.setattr(tasks.github, "get_pr",
                        lambda o, r, n, token=None: {"state": "closed", "merged": False})
    with SyncSession() as db:
        p = db.get(Project, pid)
        p.dev_branch = "docs/add-dev-meta-ai"
        p.dev_pr_number = 9
        row = db.get(DevRun, run_id)
        dev_concurrency.bind_run(p, row)
        tasks._reset_stale_branch(db, p, GH_TARGET)
        assert p.dev_branch is None and row.branch is None


def test_reset_stale_branch_keeps_an_open_pr(project, monkeypatch):
    pid, _, run_id, _ = project
    monkeypatch.setattr(tasks, "_project_repo_token", lambda db, p, prov, uri=None: "tok")
    monkeypatch.setattr(tasks.github, "branch_exists",
                        lambda o, r, b, token=None: True)
    monkeypatch.setattr(tasks.github, "get_pr",
                        lambda o, r, n, token=None: {"state": "open", "merged": False})
    with SyncSession() as db:
        p = db.get(Project, pid)
        p.dev_branch = "docs/add-dev-meta-ai"
        p.dev_pr_number = 9
        row = db.get(DevRun, run_id)
        dev_concurrency.bind_run(p, row)
        tasks._reset_stale_branch(db, p, GH_TARGET)
        assert p.dev_branch == "docs/add-dev-meta-ai"
        assert row.branch == "docs/add-dev-meta-ai" and row.pr_number == 9


# ---------------------------------------------------------------- classifier

def _post(db, pid, thread, body, author="admin"):
    m = Message(project_id=pid, thread=thread, author=author, body=body)
    db.add(m)
    db.commit()
    return m.id


def test_thread_feedback_on_an_open_pr_starts_another_pass(project, monkeypatch):
    """The prod repro: an admin's 'fix the MR' in the request thread."""
    pid, rid, run_id, _ = project
    dispatched = {}
    monkeypatch.setattr(tasks.settings, "chat_classify_enabled", True)
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, p: ("u", "k", "m"))
    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent",
                        lambda *a, **kw: {"intent": "revise"})
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **kw: dispatched.update(kw.get("kwargs") or {}))
    with SyncSession() as db:
        mid = _post(db, pid, f"request:{rid}", "Actually this is a Paid solution : fix the MR")

    tasks._classify_chat_message(pid, mid, {})

    assert dispatched.get("fix_only") is True, "the message must start another pass"
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.status == "development"
        bodies = [m.body for m in db.execute(select(Message).where(
            Message.project_id == pid, Message.author == "agent")).scalars().all()]
        assert any("same pull request" in b for b in bodies), "and say so in the thread"
        assert db.get(DevRun, run_id).state == "superseded"


def test_a_resume_verdict_also_revises_when_that_is_the_available_action(project, monkeypatch):
    """The model calls it `resume` as often as `revise` - both act here, since
    'try again' on an open PR means the same thing to the person writing it."""
    pid, rid, _, _ = project
    dispatched = {}
    monkeypatch.setattr(tasks.settings, "chat_classify_enabled", True)
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, p: ("u", "k", "m"))
    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent",
                        lambda *a, **kw: {"intent": "resume"})
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **kw: dispatched.update(kw.get("kwargs") or {}))
    with SyncSession() as db:
        mid = _post(db, pid, f"request:{rid}", "no, redo it please")
    tasks._classify_chat_message(pid, mid, {})
    assert dispatched.get("fix_only") is True


def test_main_thread_feedback_on_an_open_pr_starts_another_pass(project, monkeypatch):
    pid, _, _, _ = project
    dispatched = {}
    monkeypatch.setattr(tasks.settings, "chat_classify_enabled", True)
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, p: ("u", "k", "m"))
    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent",
                        lambda *a, **kw: {"intent": "revise"})
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **kw: dispatched.update(kw.get("kwargs") or {}))
    with SyncSession() as db:
        mid = _post(db, pid, "main", "actually make it paid, fix the PR", author="customer")
    tasks._classify_chat_message(pid, mid, {})
    assert dispatched.get("fix_only") is True


def test_revise_never_fires_without_an_open_pr(project, monkeypatch):
    """A plain in-flight run is still 'wait for it' - only an open PR is revisable."""
    pid, rid, run_id, _ = project
    monkeypatch.setattr(tasks.settings, "chat_classify_enabled", True)
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, p: ("u", "k", "m"))
    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent",
                        lambda *a, **kw: {"intent": "revise"})
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **kw: pytest.fail("must not dispatch mid-run"))
    with SyncSession() as db:
        db.get(Project, pid).dev_run_state = "running"
        db.get(DevRun, run_id).state = "running"
        db.commit()
        mid = _post(db, pid, f"request:{rid}", "fix the MR")
    tasks._classify_chat_message(pid, mid, {})
