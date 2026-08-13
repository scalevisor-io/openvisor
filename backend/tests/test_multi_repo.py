"""§multi-repo: provider detection, push-target selection, per-repo auto-merge
auth check, and the customer-GitLab auto-merge (the GitLab sibling of the GitHub
flow). Unit + DB (SyncSession) coverage, plus one HTTP pass over the repo
endpoints (connect / push-target exactly-one / verify-auth gate / remove).
"""
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.core.security import hash_password
from app.main import app
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, ProjectMemory,
    ProjectRepo, StatusChange, User,
)
from app.services import events, github, gitlab, repos as repolib
from app.services.llm import LLMUnavailable
from app.workers import tasks
from app.workers.celery_app import celery

GH_REMOTE = "git@github.com:acme/widgets.git"
GL_REMOTE = "git@gitlab.com:acme/widgets.git"
GL_TARGET = {"provider": "gitlab", "customer": True, "remote": GL_REMOTE,
             "runner_provider": "gitlab_customer", "base_url": "https://gitlab.com",
             "path": "acme/widgets", "base_branch": "main", "auto_merge": True,
             "repo_id": None}
OTHER_REMOTE = "git@git.example.org:acme/widgets.git"


# ---------------------------------------------------------------- provider detection

def test_detect_provider_from_url(monkeypatch):
    monkeypatch.setattr(gitlab.settings, "gitlab_url", "https://gitlab.internal.acme.io")
    assert repolib.detect_provider("git@github.com:o/r.git") == "github"
    assert repolib.detect_provider("https://github.com/o/r") == "github"
    assert repolib.detect_provider("git@gitlab.com:o/r.git") == "gitlab"
    assert repolib.detect_provider("https://gitlab.example.com/o/r.git") == "gitlab"
    # the configured platform host is recognised as gitlab
    assert repolib.detect_provider("git@gitlab.internal.acme.io:o/r.git") == "gitlab"
    # an unrecognised self-hosted host stays 'other' (UI asks the user to pick)
    assert repolib.detect_provider("git@git.example.org:o/r.git") == "other"
    assert repolib.detect_provider(None) == "other"


def test_gitlab_url_helpers():
    assert gitlab.parse_repo_path("git@gitlab.com:acme/widgets.git") == "acme/widgets"
    assert gitlab.parse_repo_path("https://gitlab.com/acme/sub/widgets.git") == "acme/sub/widgets"
    assert gitlab.customer_base_url("git@gitlab.example.com:o/r.git") == "https://gitlab.example.com"
    assert gitlab.customer_base_url("https://gitlab.com/o/r") == "https://gitlab.com"
    assert gitlab._host("git@github.com:o/r.git") == "github.com"


def test_token_key():
    assert repolib.token_key("github") == "GITHUB_TOKEN"
    assert repolib.token_key("gitlab") == "GITLAB_TOKEN"
    assert repolib.token_key("other") is None


def test_check_auth_dispatch(monkeypatch):
    monkeypatch.setattr(github, "check_repo_access", lambda o, r, t: (True, "gh ok"))
    monkeypatch.setattr(gitlab, "check_repo_access", lambda b, p, t: (True, "gl ok"))
    assert repolib.check_auth("github", GH_REMOTE, "tok") == (True, "gh ok")
    assert repolib.check_auth("gitlab", GL_REMOTE, "tok") == (True, "gl ok")
    ok, detail = repolib.check_auth("other", OTHER_REMOTE, "tok")
    assert ok is False and "GitHub or GitLab" in detail


def test_check_repo_access_no_token():
    assert github.check_repo_access("o", "r", "")[0] is False
    assert gitlab.check_repo_access("https://gitlab.com", "o/r", "")[0] is False


# ---------------------------------------------------------------- fixtures / helpers

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
        o = Organization(name="Multi-repo Org", credit_balance=100.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
                db.execute(delete(ProjectMemory).where(ProjectMemory.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _project(oid, *, repos=(), gitlab_platform=False, token_memory=None, **kw):
    """repos = [(ssh_uri, provider, is_push_target, auto_merge)]."""
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "running")
    kw.setdefault("ssh_private_key_enc", encrypt("PRIVATE-KEY-BODY"))
    if gitlab_platform:
        kw.setdefault("gitlab_ssh_url", "git@gitlab.platform:grp/p.git")
        kw.setdefault("gitlab_project_id", 42)
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.flush()
        for i, (uri, prov, push, am) in enumerate(repos):
            db.add(ProjectRepo(project_id=p.id, ssh_uri=uri,
                               role="primary" if i == 0 else "secondary",
                               provider=prov, is_push_target=push, auto_merge=am))
        if token_memory:
            for key, val in token_memory.items():
                db.add(ProjectMemory(project_id=p.id, author="customer", key=key,
                                     value_enc=encrypt(val), is_secret=True))
        db.commit()
        return p.id


# ---------------------------------------------------------------- _dev_target

def test_dev_target_picks_push_repo(org):
    pid = _project(org, repos=[
        (GH_REMOTE, "github", False, False),
        (GL_REMOTE, "gitlab", True, True),
    ])
    with SyncSession() as db:
        t = tasks._dev_target(db, db.get(Project, pid))
    assert t["provider"] == "gitlab" and t["customer"] is True
    assert t["path"] == "acme/widgets" and t["base_url"] == "https://gitlab.com"
    assert t["auto_merge"] is True and t["runner_provider"] == "gitlab_customer"


def test_dev_target_switch_push_repo(org):
    pid = _project(org, repos=[
        (GH_REMOTE, "github", True, False),
        (GL_REMOTE, "gitlab", False, False),
    ])
    with SyncSession() as db:
        assert tasks._dev_target(db, db.get(Project, pid))["provider"] == "github"
    # flip the push flag to the gitlab repo
    with SyncSession() as db:
        rows = db.execute(select(ProjectRepo).where(ProjectRepo.project_id == pid)).scalars().all()
        for r in rows:
            r.is_push_target = r.provider == "gitlab"
        db.commit()
    with SyncSession() as db:
        assert tasks._dev_target(db, db.get(Project, pid))["provider"] == "gitlab"


def test_dev_target_platform_fallback_when_none_push(org):
    # connected repos but none is push → the platform GitLab repo is the target
    pid = _project(org, gitlab_platform=True,
                   repos=[(GH_REMOTE, "github", False, False)])
    with SyncSession() as db:
        t = tasks._dev_target(db, db.get(Project, pid))
    assert t["provider"] == "gitlab" and t["customer"] is False
    assert t["remote"] == "git@gitlab.platform:grp/p.git"


def test_dev_target_squash_default_on_and_per_repo(org):
    # squash_on_merge defaults ON and rides into the target; the platform repo
    # always squashes (no checkbox - we own that host)
    pid = _project(org, gitlab_platform=True, repos=[(GH_REMOTE, "github", True, False)])
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert tasks._dev_target(db, p)["squash"] is True
        db.execute(select(ProjectRepo).where(ProjectRepo.project_id == pid)) \
          .scalar_one().squash_on_merge = False
        db.commit()
        assert tasks._dev_target(db, p)["squash"] is False
        # platform fallback
        db.execute(delete(ProjectRepo).where(ProjectRepo.project_id == pid))
        db.commit()
        db.expire_all()
        assert tasks._dev_target(db, db.get(Project, pid))["squash"] is True


def test_dev_target_summarize_to_issue_default_off_and_per_repo(org):
    # summarize_to_issue defaults OFF and rides into the target; the platform
    # fallback never summarizes (no issue watch on the platform repo)
    pid = _project(org, gitlab_platform=True, repos=[(GH_REMOTE, "github", True, False)])
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert tasks._dev_target(db, p)["summarize_to_issue"] is False
        db.execute(select(ProjectRepo).where(ProjectRepo.project_id == pid)) \
          .scalar_one().summarize_to_issue = True
        db.commit()
        assert tasks._dev_target(db, p)["summarize_to_issue"] is True
        db.execute(delete(ProjectRepo).where(ProjectRepo.project_id == pid))
        db.commit()
        db.expire_all()
        assert tasks._dev_target(db, db.get(Project, pid))["summarize_to_issue"] is False


def test_remote_ops_merge_honours_squash(monkeypatch):
    seen = {}
    monkeypatch.setattr(tasks.github, "merge_pr",
                        lambda o, r, n, method="squash", token=None:
                        seen.update(gh=method) or (True, "merged"))
    monkeypatch.setattr(tasks.gitlab, "customer_merge_mr",
                        lambda b, t, p, iid, squash=True:
                        seen.update(gl=squash) or (True, "merged"))
    gh_target = {"provider": "github", "owner": "acme", "repo": "widgets",
                 "base_branch": "main", "squash": True}
    tasks._remote_ops(gh_target, "tok")["merge"](1)
    assert seen["gh"] == "squash"
    tasks._remote_ops({**gh_target, "squash": False}, "tok")["merge"](1)
    assert seen["gh"] == "merge"
    tasks._remote_ops({**GL_TARGET, "squash": False}, "tok")["merge"](5)
    assert seen["gl"] is False
    tasks._remote_ops(GL_TARGET, "tok")["merge"](5)  # absent key → squash (default)
    assert seen["gl"] is True


def test_dev_target_other_host(org):
    pid = _project(org, repos=[(OTHER_REMOTE, "other", True, False)])
    with SyncSession() as db:
        t = tasks._dev_target(db, db.get(Project, pid))
    assert t["provider"] == "other" and t["remote"] == OTHER_REMOTE


def test_project_repo_token_resolution(org, monkeypatch):
    pid = _project(org, repos=[(GL_REMOTE, "gitlab", True, True)],
                   token_memory={"GITLAB_TOKEN": "glpat_x"})
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert tasks._project_repo_token(db, p, "gitlab") == "glpat_x"
        # gitlab has NO platform fallback (that token is for the platform host)
        monkeypatch.setattr(tasks.settings, "github_token", "ghp_platform")
        pid2 = _project(org, repos=[(GL_REMOTE, "gitlab", True, False)])
        p2 = db.get(Project, pid2)
        assert tasks._project_repo_token(db, p2, "gitlab") is None
        # github falls back to the platform token
        assert tasks._project_repo_token(db, p2, "github") == "ghp_platform"


# ---------------------------------------------------------------- customer GitLab auto-merge

def _gl_ops_patch(monkeypatch, *, diff="+x\n", merge=(True, "merged")):
    monkeypatch.setattr(tasks.gitlab, "customer_mr_diff", lambda *a, **k: diff)
    calls = []
    monkeypatch.setattr(tasks.gitlab, "customer_merge_mr",
                        lambda b, t, p, iid, squash=True: calls.append((b, t, p, iid)) or merge)
    monkeypatch.setattr(tasks, "_bill_dev_run", lambda db, p: None)
    monkeypatch.setattr(tasks, "_verify_boot", lambda db, p: (True, ""))
    return calls


def test_gitlab_customer_auto_merge_clean_merges(org, monkeypatch, quiet):
    pid = _project(org, repos=[(GL_REMOTE, "gitlab", True, True)], dev_pr_number=5)
    merge_calls = _gl_ops_patch(monkeypatch)
    monkeypatch.setattr(tasks.pipeline, "run_security_review",
                        lambda db, p, diff: {"verdict": "pass", "findings": [], "floor": []})
    deployed = []
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: deployed.append(a))
    with SyncSession() as db:
        p = db.get(Project, pid)
        ops = tasks._remote_ops(GL_TARGET, "glpat_x")
        tasks._remote_auto_merge(db, p, GL_TARGET, ops,
                                 {"number": 5, "url": "http://mr/5"}, "main", None, "logs")
    assert merge_calls == [("https://gitlab.com", "glpat_x", "acme/widgets", 5)]
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "deploying"
        assert p.dev_security_review["merged"] is True
    assert deployed


def test_gitlab_customer_auto_merge_fix_loop(org, monkeypatch, quiet):
    pid = _project(org, repos=[(GL_REMOTE, "gitlab", True, True)], dev_pr_number=5)
    _gl_ops_patch(monkeypatch)
    reviews = iter([
        {"verdict": "changes_requested",
         "findings": [{"severity": "high", "issue": "SQL injection", "file": "a.py", "line": 1}],
         "floor": []},
        {"verdict": "pass", "findings": [], "floor": []},
    ])
    monkeypatch.setattr(tasks.pipeline, "run_security_review", lambda db, p, diff: next(reviews))
    dispatched = []
    monkeypatch.setattr(tasks, "_dispatch_runner",
                        lambda db, p, target, fix_instruction=None, **k: dispatched.append(fix_instruction) or {"exit_code": "0", "logs": "fixed"})
    with SyncSession() as db:
        p = db.get(Project, pid)
        ops = tasks._remote_ops(GL_TARGET, "glpat_x")
        tasks._remote_auto_merge(db, p, GL_TARGET, ops,
                                 {"number": 5, "url": "http://mr/5"}, "main", None, "logs")
    assert len(dispatched) == 1 and "SQL injection" in dispatched[0]
    assert "merge request" in dispatched[0]  # provider-correct wording in the fix task
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"


def test_gitlab_customer_review_error_fails_closed(org, monkeypatch, quiet):
    pid = _project(org, repos=[(GL_REMOTE, "gitlab", True, True)], dev_pr_number=5)
    _gl_ops_patch(monkeypatch)
    merged = []
    monkeypatch.setattr(tasks.gitlab, "customer_merge_mr",
                        lambda *a, **k: merged.append(1) or (True, "merged"))

    def boom(db, p, diff):
        raise LLMUnavailable("review down")

    monkeypatch.setattr(tasks.pipeline, "run_security_review", boom)
    with SyncSession() as db:
        p = db.get(Project, pid)
        ops = tasks._remote_ops(GL_TARGET, "glpat_x")
        tasks._remote_auto_merge(db, p, GL_TARGET, ops,
                                 {"number": 5, "url": "http://mr/5"}, "main", None, "logs")
    assert merged == []  # a diff we couldn't review is never merged
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"
        assert p.status == "awaiting_customer"
        assert p.dev_security_review["verdict"] == "review_unavailable"


def _mock_build_ok(monkeypatch):
    monkeypatch.setattr(tasks.settings, "openhands_enabled", False)
    monkeypatch.setattr(tasks, "_scaffold_placeholder", lambda p: None)
    monkeypatch.setattr(tasks, "_dispatch_runner",
                        lambda db, p, target, fix_instruction=None, **k: {"exit_code": "0", "logs": "built"})
    monkeypatch.setattr(tasks, "_verify_boot", lambda db, p: (True, ""))
    monkeypatch.setattr(tasks, "_bill_dev_run", lambda db, p: None)


def test_gitlab_customer_no_token_parks_branch(org, monkeypatch, quiet):
    pid = _project(org, repos=[(GL_REMOTE, "gitlab", True, False)])
    _mock_build_ok(monkeypatch)
    opened = []
    monkeypatch.setattr(tasks.gitlab, "customer_open_mr", lambda *a, **k: opened.append(1) or {})
    target = {**GL_TARGET, "auto_merge": False}
    with SyncSession() as db:
        tasks._run_development_customer(db, db.get(Project, pid), target, fix_only=False)
    assert opened == []  # no token → never opens an MR
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"
        assert p.status == "awaiting_customer"
        assert p.dev_pr_number is None
    assert any("merge request" in ev["message"]["body"].lower()
               for _pid, ev in quiet if ev.get("type") == "message")


def test_other_host_parks_branch(org, monkeypatch, quiet):
    pid = _project(org, repos=[(OTHER_REMOTE, "other", True, False)])
    _mock_build_ok(monkeypatch)
    target = {"provider": "other", "remote": OTHER_REMOTE, "base_branch": "main",
              "auto_merge": False, "repo_id": None}
    with SyncSession() as db:
        tasks._run_development_customer(db, db.get(Project, pid), target, fix_only=False)
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"
        assert p.dev_pr_number is None


def test_push_failed_detection():
    sentinel = "runner: PUSH_FAILED - the branch was not published"
    assert tasks._push_failed({"exit_code": "4", "logs": sentinel}) is True
    # a driver error is the deeper failure: the generic runner-error branch owns it
    assert tasks._push_failed({"exit_code": "1", "logs": sentinel}) is False
    assert tasks._push_failed({"exit_code": "4", "logs": "runner: pushed"}) is False
    assert tasks._push_failed(None) is False


def test_push_failed_parks_resumable(org, monkeypatch, quiet):
    pid = _project(org, repos=[(GL_REMOTE, "gitlab", True, False)])
    monkeypatch.setattr(tasks.settings, "openhands_enabled", False)
    monkeypatch.setattr(tasks, "_scaffold_placeholder", lambda p: None)
    monkeypatch.setattr(tasks, "_bill_dev_run", lambda db, p: None)
    monkeypatch.setattr(
        tasks, "_dispatch_runner",
        lambda db, p, target, fix_instruction=None, **k:
        {"exit_code": "4", "logs": "runner: PUSH_FAILED - the branch was not published"})
    booted = []
    monkeypatch.setattr(tasks, "_verify_boot", lambda db, p: booted.append(1) or (True, ""))
    opened = []
    monkeypatch.setattr(tasks.gitlab, "customer_open_mr", lambda *a, **k: opened.append(1) or {})
    target = {**GL_TARGET, "auto_merge": False}
    with SyncSession() as db:
        tasks._run_development_customer(db, db.get(Project, pid), target, fix_only=False)
    assert booted == [] and opened == []  # parked before the boot gate / publish
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "failed"
        assert p.status == "awaiting_customer"
        assert p.dev_run_error == "Pushing the branch failed"
    assert any("pushing the branch" in ev["message"]["body"].lower()
               for _pid, ev in quiet if ev.get("type") == "message")


def test_dev_pr_sweep_closed_pr_ends_the_work_unit(org, monkeypatch, quiet):
    # A PR closed WITHOUT merging is rejected work: the sweep parks the run
    # failed AND clears the branch + PR pointer on the project and the parked
    # row, so a Resume derives a fresh branch and opens a NEW pull request.
    pid = _project(org, repos=[(GH_REMOTE, "github", True, False)],
                   token_memory={"GITHUB_TOKEN": "ghp_x"},
                   dev_run_state="awaiting_merge", status="awaiting_customer",
                   dev_branch="feat/x", dev_pr_number=7,
                   dev_pr_url="https://github.com/acme/widgets/pull/7")
    with SyncSession() as db:
        run = DevRun(project_id=pid, state="awaiting_merge", branch="feat/x",
                     pr_number=7, pr_url="https://github.com/acme/widgets/pull/7")
        db.add(run)
        db.commit()
        run_id = run.id
    monkeypatch.setattr(tasks.github, "get_pr",
                        lambda o, r, n, token=None: {"state": "closed", "merged": False,
                                                     "base": {"ref": "main"},
                                                     "head": {"sha": "h"}})
    monkeypatch.setattr(tasks.github, "commits_contained_in", lambda *a, **k: False)
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "failed"
        assert (p.dev_branch, p.dev_pr_number, p.dev_pr_url) == (None, None, None)
        row = db.get(DevRun, run_id)
        assert row.state == "failed"
        assert (row.branch, row.pr_number, row.pr_url) == (None, None, None)
        db.execute(delete(DevRun).where(DevRun.project_id == pid))
        db.commit()
    assert any("closed without" in ev["message"]["body"]
               for _pid, ev in quiet if ev.get("type") == "message")


def test_dev_pr_sweep_gitlab_merged_deploys(org, monkeypatch, quiet):
    pid = _project(org, repos=[(GL_REMOTE, "gitlab", True, False)],
                   token_memory={"GITLAB_TOKEN": "glpat_x"},
                   dev_run_state="awaiting_merge", status="awaiting_customer", dev_pr_number=5)
    monkeypatch.setattr(tasks.gitlab, "customer_get_mr",
                        lambda b, t, p, iid: {"state": "merged"})
    deployed = []
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: deployed.append(a))
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
    assert deployed


# ---------------------------------------------------------------- SSH reachability (check_ssh)

class _Proc:
    def __init__(self, returncode, stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, "", stderr


def _patch_lsremote(monkeypatch, *, returncode=0, stderr="", raises=None):
    def fake_run(args, **kw):
        if raises is not None:
            raise raises
        return _Proc(returncode, stderr)
    monkeypatch.setattr(repolib.subprocess, "run", fake_run)


def test_is_ssh_uri():
    assert repolib.is_ssh_uri("git@github.com:o/r.git")
    assert repolib.is_ssh_uri("ssh://git@host:22/o/r.git")
    assert not repolib.is_ssh_uri("https://github.com/o/r.git")
    assert not repolib.is_ssh_uri("http://host/o/r.git")
    assert not repolib.is_ssh_uri("")
    assert not repolib.is_ssh_uri(None)


def test_check_ssh_success(monkeypatch):
    _patch_lsremote(monkeypatch, returncode=0)
    ok, detail = repolib.check_ssh(GH_REMOTE, "PRIVATE-KEY-BODY")
    assert ok is True and "deploy key has access" in detail.lower()


def test_check_ssh_permission_denied(monkeypatch):
    _patch_lsremote(monkeypatch, returncode=128,
                    stderr="git@github.com: Permission denied (publickey).\n"
                           "fatal: Could not read from remote repository.")
    ok, detail = repolib.check_ssh(GH_REMOTE, "K")
    assert ok is False and "deploy key" in detail.lower() and "add" in detail.lower()


def test_check_ssh_host_unreachable(monkeypatch):
    _patch_lsremote(monkeypatch, returncode=128,
                    stderr="ssh: Could not resolve hostname nope.example: Name or service not known")
    ok, detail = repolib.check_ssh("git@nope.example:o/r.git", "K")
    assert ok is False and "reach the host" in detail.lower()


def test_check_ssh_timeout(monkeypatch):
    _patch_lsremote(monkeypatch, raises=subprocess.TimeoutExpired("git", 15))
    ok, detail = repolib.check_ssh(GH_REMOTE, "K")
    assert ok is False and "timed out" in detail.lower()


def test_check_ssh_https_is_skipped(monkeypatch):
    # An https:// remote never uses the deploy key: return the note without shelling out.
    called = []
    monkeypatch.setattr(repolib.subprocess, "run", lambda *a, **k: called.append(a))
    ok, detail = repolib.check_ssh("https://github.com/o/r.git", "K")
    assert ok is False and not called and "https" in detail.lower()


def test_check_ssh_no_key():
    ok, detail = repolib.check_ssh(GH_REMOTE, "")
    assert ok is False and "deploy key" in detail.lower()


def test_check_ssh_bad_url(monkeypatch):
    called = []
    monkeypatch.setattr(repolib.subprocess, "run", lambda *a, **k: called.append(a))
    ok, _ = repolib.check_ssh("not a url", "K")
    assert ok is False and not called


# ---------------------------------------------------------------- HTTP repo endpoints

@pytest.fixture(scope="module")
def client():
    # Module-scoped so every request shares one event loop. Reset the async engine
    # pool first (close=False abandons, never awaits, connections an earlier HTTP
    # test module left bound to its now-closed loop) to avoid "Event loop is closed".
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    with TestClient(app) as c:
        yield c


def _customer(org_id):
    email = f"repoui-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "customer-secret-123"
    with SyncSession() as db:
        u = User(org_id=org_id, email=email, password_hash=hash_password(pwd),
                 role="customer", email_verified=True)
        db.add(u)
        p = Project(org_id=org_id, name="RP", description="d", kind="ai", status="draft",
                    ssh_public_key="ssh-ed25519 AAAA test", ssh_private_key_enc=encrypt("K"))
        db.add(p)
        db.commit()
        return email, pwd, p.id


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]  # CSRF cookie + token
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_repo_endpoints_flow(org, client, monkeypatch):
    email, pwd, pid = _customer(org)
    h = _auth(client, email, pwd)

    # connect a github + a gitlab repo; first becomes the push target
    r = client.post(f"/api/projects/{pid}/repos", json={"ssh_uri": GH_REMOTE}, headers=h)
    assert r.status_code == 201
    repos = r.json()["repos"]
    assert len(repos) == 1 and repos[0]["provider"] == "github" and repos[0]["is_push_target"]
    r = client.post(f"/api/projects/{pid}/repos", json={"ssh_uri": GL_REMOTE}, headers=h)
    body = r.json()
    gh = next(x for x in body["repos"] if x["provider"] == "github")
    gl = next(x for x in body["repos"] if x["provider"] == "gitlab")
    assert gh["is_push_target"] and not gl["is_push_target"]  # push stays on the first

    # switch push target to the gitlab repo → exactly one push target
    r = client.patch(f"/api/projects/{pid}/repos/{gl['id']}",
                     json={"is_push_target": True}, headers=h)
    reps = r.json()["repos"]
    assert sum(1 for x in reps if x["is_push_target"]) == 1
    assert next(x for x in reps if x["id"] == gl["id"])["is_push_target"]

    # enabling auto_merge is blocked while the auth check fails
    monkeypatch.setattr(repolib, "check_auth", lambda prov, uri, tok: (False, "bad token"))
    r = client.patch(f"/api/projects/{pid}/repos/{gl['id']}",
                     json={"auto_merge": True}, headers=h)
    assert r.status_code == 409 and "bad token" in r.json()["detail"]
    assert not any(x["auto_merge"] for x in client.get(f"/api/projects/{pid}", headers=h).json()["repos"])

    # verify-auth reflects the check result
    assert client.post(f"/api/projects/{pid}/repos/{gl['id']}/verify-auth", headers=h).json()["ok"] is False
    monkeypatch.setattr(repolib, "check_auth", lambda prov, uri, tok: (True, "ok"))
    assert client.post(f"/api/projects/{pid}/repos/{gl['id']}/verify-auth", headers=h).json()["ok"] is True

    # now auto_merge saves (auth check passes)
    r = client.patch(f"/api/projects/{pid}/repos/{gl['id']}",
                     json={"auto_merge": True}, headers=h)
    assert r.status_code == 200
    assert next(x for x in r.json()["repos"] if x["id"] == gl["id"])["auto_merge"]

    # squash_on_merge: defaults on, checkbox off/on round-trips (no auth gate)
    assert next(x for x in r.json()["repos"] if x["id"] == gl["id"])["squash_on_merge"]
    r = client.patch(f"/api/projects/{pid}/repos/{gl['id']}",
                     json={"squash_on_merge": False}, headers=h)
    assert r.status_code == 200
    assert next(x for x in r.json()["repos"] if x["id"] == gl["id"])["squash_on_merge"] is False

    # summarize_to_issue: defaults off, round-trips (no auth gate)
    assert next(x for x in r.json()["repos"] if x["id"] == gl["id"])["summarize_to_issue"] is False
    r = client.patch(f"/api/projects/{pid}/repos/{gl['id']}",
                     json={"summarize_to_issue": True}, headers=h)
    assert r.status_code == 200
    assert next(x for x in r.json()["repos"] if x["id"] == gl["id"])["summarize_to_issue"] is True

    # removing the push repo promotes the remaining one
    r = client.request("DELETE", f"/api/projects/{pid}/repos/{gl['id']}", headers=h)
    reps = r.json()["repos"]
    assert len(reps) == 1 and reps[0]["provider"] == "github" and reps[0]["is_push_target"]


def test_verify_ssh_endpoint(org, client, monkeypatch):
    email, pwd, pid = _customer(org)
    h = _auth(client, email, pwd)
    r = client.post(f"/api/projects/{pid}/repos", json={"ssh_uri": GH_REMOTE}, headers=h)
    rid = r.json()["repos"][0]["id"]

    # the endpoint surfaces whatever check_ssh decides, in {ok, detail} shape
    monkeypatch.setattr(repolib, "check_ssh", lambda uri, key: (True, "reachable"))
    body = client.post(f"/api/projects/{pid}/repos/{rid}/verify-ssh", headers=h).json()
    assert body == {"ok": True, "detail": "reachable"}
    monkeypatch.setattr(repolib, "check_ssh", lambda uri, key: (False, "nope"))
    body = client.post(f"/api/projects/{pid}/repos/{rid}/verify-ssh", headers=h).json()
    assert body["ok"] is False and body["detail"] == "nope"

    # the project's deploy key is decrypted and passed through (never the ciphertext)
    seen = {}
    monkeypatch.setattr(repolib, "check_ssh",
                        lambda uri, key: seen.update(uri=uri, key=key) or (True, "ok"))
    client.post(f"/api/projects/{pid}/repos/{rid}/verify-ssh", headers=h)
    assert seen["uri"] == GH_REMOTE and seen["key"] == "K"  # _customer seeds encrypt("K")

    # unknown repo id → 404
    assert client.post(f"/api/projects/{pid}/repos/nope/verify-ssh", headers=h).status_code == 404


def test_verify_ssh_ownership_guarded(org, client):
    # a different org's customer can't reach this project's repo (404, not the result)
    email, pwd, pid = _customer(org)
    h = _auth(client, email, pwd)
    rid = client.post(f"/api/projects/{pid}/repos", json={"ssh_uri": GH_REMOTE},
                      headers=h).json()["repos"][0]["id"]
    with SyncSession() as db:
        other = Organization(name="Other Org", credit_balance=0.0)
        db.add(other)
        db.commit()
        oid2 = other.id
    email2, pwd2, _ = _customer(oid2)
    h2 = _auth(client, email2, pwd2)
    try:
        assert client.post(f"/api/projects/{pid}/repos/{rid}/verify-ssh",
                           headers=h2).status_code == 404
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid2)).scalars().all()
            if pids:
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
            db.execute(delete(User).where(User.org_id == oid2))
            db.execute(delete(Project).where(Project.org_id == oid2))
            db.execute(delete(Organization).where(Organization.id == oid2))
            db.commit()


def test_auto_merge_rejected_on_other_repo(org, client):
    email, pwd, pid = _customer(org)
    h = _auth(client, email, pwd)
    r = client.post(f"/api/projects/{pid}/repos", json={"ssh_uri": OTHER_REMOTE}, headers=h)
    rid = r.json()["repos"][0]["id"]
    assert r.json()["repos"][0]["can_auto_merge"] is False
    r = client.patch(f"/api/projects/{pid}/repos/{rid}", json={"auto_merge": True}, headers=h)
    assert r.status_code == 422
