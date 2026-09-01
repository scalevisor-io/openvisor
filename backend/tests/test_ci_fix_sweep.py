"""§14.10 CI watch: dev_pr_sweep notices a FAILED pipeline on the open change of
an awaiting_merge run and chains a scoped CI-fix run onto it (the §revise
mechanics), bounded by CI_MAX_RETRIES per work unit. Green/pending CI, an empty
wallet and an exhausted budget must all leave the row awaiting_merge - the
customer's own merge keeps deploying through the sweep either way.
"""
import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, ProjectMemory,
    ProjectRepo, StatusChange, User,
)
from app.services import events
from app.workers import tasks
from app.workers.celery_app import celery

GH_REMOTE = "git@github.com:acme/widgets.git"
GL_REMOTE = "git@gitlab.com:acme/widgets.git"
PR_URL = "https://github.com/acme/widgets/pull/7"
MR_URL = "https://gitlab.com/acme/widgets/-/merge_requests/5"


@pytest.fixture
def quiet(monkeypatch):
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(celery, "send_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: None)
    return ws


@pytest.fixture
def dispatched(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda *a, **k: calls.append(k))
    return calls


@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="CI-watch Org", credit_balance=100.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
                db.execute(delete(ProjectMemory).where(ProjectMemory.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _parked_project(oid, provider="github", attempts=0):
    """A project parked awaiting_merge on an open change, with its ledger row."""
    gh = provider == "github"
    with SyncSession() as db:
        p = Project(org_id=oid, name="P", description="d", kind="ai",
                    status="awaiting_customer", dev_run_state="awaiting_merge",
                    dev_branch="feat/x", dev_pr_number=7 if gh else 5,
                    dev_pr_url=PR_URL if gh else MR_URL,
                    ssh_private_key_enc=encrypt("PRIVATE-KEY-BODY"))
        db.add(p)
        db.flush()
        db.add(ProjectRepo(project_id=p.id, ssh_uri=GH_REMOTE if gh else GL_REMOTE,
                           role="primary", provider=provider,
                           is_push_target=True, auto_merge=False))
        db.add(ProjectMemory(project_id=p.id, author="customer",
                             key="GITHUB_TOKEN" if gh else "GITLAB_TOKEN",
                             value_enc=encrypt("tok_x"), is_secret=True))
        run = DevRun(project_id=p.id, state="awaiting_merge", branch="feat/x",
                     pr_number=7 if gh else 5, pr_url=PR_URL if gh else MR_URL,
                     ci_fix_attempts=attempts)
        db.add(run)
        db.commit()
        return p.id, run.id


def _open_pr(monkeypatch, sha="abc123"):
    monkeypatch.setattr(tasks.github, "get_pr",
                        lambda o, r, n, token=None: {"state": "open", "merged": False,
                                                     "base": {"ref": "main"},
                                                     "head": {"sha": sha}})
    monkeypatch.setattr(tasks.github, "commits_contained_in", lambda *a, **k: False)


def _open_mr(monkeypatch, sha="abc123"):
    monkeypatch.setattr(tasks.gitlab, "customer_get_mr",
                        lambda b, t, p, iid: {"state": "opened", "sha": sha})
    monkeypatch.setattr(tasks, "_agent_branch_merged_ssh", lambda pid, remote: False)


def _messages(quiet):
    return [ev["message"]["body"] for _pid, ev in quiet if ev.get("type") == "message"]


def test_failed_ci_chains_a_fix_run(org, monkeypatch, quiet, dispatched):
    pid, run_id = _parked_project(org)
    _open_pr(monkeypatch)
    monkeypatch.setattr(tasks.github, "ci_status", lambda o, r, s, token=None: "failure")
    monkeypatch.setattr(tasks.github, "failed_ci_logs",
                        lambda o, r, s, token=None: "### job 'test' failed\nboom")
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        old = db.get(DevRun, run_id)
        assert old.state == "superseded"
        new = db.execute(select(DevRun).where(DevRun.predecessor_id == run_id)).scalar_one()
        assert new.state == "queued"
        assert new.ci_fix_attempts == 1
        assert (new.branch, new.pr_number, new.pr_url) == ("feat/x", 7, PR_URL)
        assert db.get(Project, pid).status == "development"
        new_id = new.id
    assert len(dispatched) == 1
    kw = dispatched[0]["kwargs"]
    assert kw["fix_only"] is True and kw["run_id"] == new_id
    assert "boom" in kw["fix_instruction"]
    assert any("attempting an automatic fix (1/" in b for b in _messages(quiet))


def test_green_or_pending_ci_is_left_alone(org, monkeypatch, quiet, dispatched):
    pid, run_id = _parked_project(org)
    _open_pr(monkeypatch)
    for status in ("pending", "success", "none"):
        monkeypatch.setattr(tasks.github, "ci_status",
                            lambda o, r, s, token=None, _st=status: _st)
        tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(DevRun, run_id).state == "awaiting_merge"
        assert db.get(Project, pid).status == "awaiting_customer"
    assert dispatched == []


def test_exhausted_attempts_post_one_message_and_keep_watching(org, monkeypatch,
                                                               quiet, dispatched):
    pid, run_id = _parked_project(org, attempts=tasks.settings.ci_max_retries)
    _open_pr(monkeypatch)
    monkeypatch.setattr(tasks.github, "ci_status", lambda o, r, s, token=None: "failure")
    monkeypatch.setattr(tasks.github, "failed_ci_logs", lambda o, r, s, token=None: "x")
    tasks.dev_pr_sweep()
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        row = db.get(DevRun, run_id)
        # cap+1 marks "exhaustion posted"; the row keeps its merge watcher.
        assert row.state == "awaiting_merge"
        assert row.ci_fix_attempts == tasks.settings.ci_max_retries + 1
    assert dispatched == []
    exhausted = [b for b in _messages(quiet) if "couldn't get the pipeline" in b]
    assert len(exhausted) == 1


def test_empty_wallet_pauses_the_fix_loop(org, monkeypatch, quiet, dispatched):
    pid, run_id = _parked_project(org)
    with SyncSession() as db:
        db.get(Organization, org).credit_balance = 0.0
        db.commit()
    _open_pr(monkeypatch)
    monkeypatch.setattr(tasks.github, "ci_status", lambda o, r, s, token=None: "failure")
    monkeypatch.setattr(tasks.github, "failed_ci_logs", lambda o, r, s, token=None: "x")
    tasks.dev_pr_sweep()
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        row = db.get(DevRun, run_id)
        assert row.state == "awaiting_merge"
        assert row.run_error == tasks._CI_FIX_PAUSED_CREDITS
        assert row.ci_fix_attempts == 0  # a top-up resumes the loop untouched
    assert dispatched == []
    assert len([b for b in _messages(quiet) if "credits are exhausted" in b]) == 1


def test_customer_gitlab_failed_pipeline_chains_a_fix(org, monkeypatch, quiet, dispatched):
    pid, run_id = _parked_project(org, provider="gitlab")
    _open_mr(monkeypatch)
    monkeypatch.setattr(tasks.gitlab, "customer_pipeline_status",
                        lambda b, t, p, sha: "failure")
    monkeypatch.setattr(tasks.gitlab, "customer_failed_pipeline_logs",
                        lambda b, t, p, iid: "### job 'lint' failed\nruff says no")
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(DevRun, run_id).state == "superseded"
        new = db.execute(select(DevRun).where(DevRun.predecessor_id == run_id)).scalar_one()
        assert new.ci_fix_attempts == 1 and new.pr_number == 5
    assert len(dispatched) == 1
    assert "ruff says no" in dispatched[0]["kwargs"]["fix_instruction"]


def test_build_and_boot_seeds_first_dispatch_with_the_ci_fix(org, monkeypatch, quiet):
    pid, _run_id = _parked_project(org)
    seen: list = []
    monkeypatch.setattr(tasks, "_stop_requested", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "_mark_dispatch_start", lambda db, p: None)
    monkeypatch.setattr(tasks, "_bill_dev_run", lambda db, p: None)
    monkeypatch.setattr(tasks, "_dispatch_runner",
                        lambda db, p, t, fix_instruction=None, **k:
                        seen.append(fix_instruction) or {"exit_code": "0", "logs": "ok"})
    monkeypatch.setattr(tasks.settings, "dev_boot_check", False)
    target = {"provider": "github", "remote": GH_REMOTE, "base_branch": "main"}
    with SyncSession() as db:
        logs = tasks._build_and_boot(db, db.get(Project, pid), target, thread="main",
                                     skip_agent=False, boot_verb="open the pull request",
                                     fix_instruction="FIX THE CI")
    assert logs == "ok"
    assert seen == ["FIX THE CI"]


# ---------------------------------------------------------------- github.ci_status
# §14.10 on partial tokens: a fine-grained PAT often grants only ONE of GitHub's
# two CI surfaces - the readable one must still produce a verdict (seen live:
# a token 403ing /commits/{sha}/status blinded the whole watch).

def _gh_transport(status_resp, checks_resp):
    import httpx

    def handler(request):
        if request.url.path.endswith("/status"):
            return status_resp() if callable(status_resp) else httpx.Response(
                status_resp[0], json=status_resp[1])
        return httpx.Response(checks_resp[0], json=checks_resp[1])
    return httpx.MockTransport(handler)


def _gh_client(monkeypatch, transport):
    import httpx

    from app.services import github
    monkeypatch.setattr(github, "_client",
                        lambda token=None: httpx.Client(
                            base_url="https://api.github.com", transport=transport))


def test_ci_status_statuses_denied_check_runs_still_answer(monkeypatch):
    from app.services import github
    _gh_client(monkeypatch, _gh_transport(
        (403, {"message": "Resource not accessible"}),
        (200, {"check_runs": [{"status": "completed", "conclusion": "failure"}]})))
    assert github.ci_status("o", "r", "sha", token="t") == "failure"


def test_ci_status_check_runs_denied_statuses_still_answer(monkeypatch):
    from app.services import github
    _gh_client(monkeypatch, _gh_transport(
        (200, {"state": "success", "total_count": 2}),
        (403, {"message": "Resource not accessible"})))
    assert github.ci_status("o", "r", "sha", token="t") == "success"


def test_ci_status_both_denied_raises_for_the_sweep_to_skip(monkeypatch):
    import httpx
    import pytest as _pytest

    from app.services import github
    _gh_client(monkeypatch, _gh_transport(
        (403, {"message": "no"}), (403, {"message": "no"})))
    with _pytest.raises(httpx.HTTPStatusError):
        github.ci_status("o", "r", "sha", token="t")
