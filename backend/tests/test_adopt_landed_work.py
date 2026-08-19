"""§14 don't-lose-landed-work: a runner that exits non-zero AFTER its branch
landed with an open PR/MR adopts the change (awaiting_merge, human review)
instead of parking failed - unless the pushed diff trips the leak scan, which
fails closed to admin review with the published-change alert.
"""
import pytest
from sqlalchemy import delete, select, update

from app.core.db import SyncSession
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, ProjectRepo,
    Request, StatusChange,
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
        org = Organization(name="AdoptWork Test Org", credit_balance=100.0)
        db.add(org)
        db.commit()
        oid = org.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(update(Project).where(Project.id.in_(pids))
                           .values(dev_request_id=None))
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


TARGET = {"provider": "github", "owner": "acme", "repo": "app",
          "base_branch": "main", "customer": True}


def _project(db, oid, tmp_path, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "running")
    kw.setdefault("dev_branch", "f/#2451-synchro-vecmilmaps")
    kw.setdefault("workspace_path", str(tmp_path))
    p = Project(org_id=oid, **kw)
    db.add(p)
    db.flush()
    req = Request(project_id=p.id, type="bug", handling="ai",
                  status="in_progress", title="Fix CI env variables")
    db.add(req)
    db.flush()
    p.dev_request_id = req.id
    db.commit()
    return p, req


def _probe(monkeypatch, *, token="tok", branch=True, pr=True,
           diff="+    TRACELIB_VERSION=4.6.0\n-    TRACELIB_VERSION=3.155.1\n"):
    monkeypatch.setattr(tasks, "_project_repo_token", lambda db, p, prov, uri=None: token)
    monkeypatch.setattr(tasks.github, "branch_exists",
                        lambda owner, repo, b, token=None: branch)
    monkeypatch.setattr(
        tasks.github, "find_open_pr",
        lambda owner, repo, head, token=None:
        {"number": 2453, "html_url": "https://github.com/acme/app/pull/2453"}
        if pr else None)
    monkeypatch.setattr(tasks, "_remote_ops",
                        lambda target, tok, br: {"diff": lambda n: diff})
    from app.services import leakscan
    monkeypatch.setattr(leakscan, "platform_secret_values", lambda *a, **k: [])
    monkeypatch.setattr(leakscan, "kb_fingerprints_from_db", lambda *a, **k: [])


def test_adoption_declines_when_nothing_landed(org_id, tmp_path, quiet, monkeypatch):
    with SyncSession() as db:
        p, _ = _project(db, org_id, tmp_path)
        _probe(monkeypatch, token=None)
        assert tasks._adopt_landed_work(db, p, TARGET, "main", "") is False
        _probe(monkeypatch, branch=False)
        assert tasks._adopt_landed_work(db, p, TARGET, "main", "") is False
        _probe(monkeypatch, pr=False)
        assert tasks._adopt_landed_work(db, p, TARGET, "main", "") is False
        assert tasks._adopt_landed_work(db, p, None, "main", "") is False
        # nothing was parked or posted by the declines
        assert p.dev_run_state == "running" and p.status == "development"


def test_adoption_parks_awaiting_merge_with_the_open_pr(org_id, tmp_path, quiet,
                                                        monkeypatch):
    """The prod regression shape: pushed onto the customer's already-open PR,
    then exit 1 - the change is adopted, never auto-merged."""
    with SyncSession() as db:
        p, req = _project(db, org_id, tmp_path)
        _probe(monkeypatch)
        assert tasks._adopt_landed_work(db, p, TARGET, "main", "the logs") is True
        assert p.dev_run_state == "awaiting_merge"
        assert p.dev_pr_number == 2453
        assert p.dev_pr_url.endswith("/pull/2453")
        assert p.status == "awaiting_customer"
        assert any(r.get("url", "").endswith("/pull/2453") for r in req.pr_urls)
        msg = db.execute(select(Message).filter_by(project_id=p.id, author="agent")
                         ).scalars().one()
        assert "work had already landed" in msg.body and "2453" in msg.body
        assert msg.meta["prs"][0]["number"] == 2453


def test_adoption_fails_closed_on_a_leaky_diff(org_id, tmp_path, quiet, monkeypatch):
    """A published change that trips the scan parks for the admin - with the
    honest copy that it is ALREADY public."""
    sent = []
    monkeypatch.setattr(tasks.emailer, "send_email",
                        lambda *a, **k: sent.append(a))
    with SyncSession() as db:
        p, _ = _project(db, org_id, tmp_path)
        _probe(monkeypatch, diff="+    API_KEY=sk-verysecretvalue1234567890abcdef\n")
        from app.services import leakscan
        monkeypatch.setattr(leakscan, "platform_secret_values",
                            lambda *a, **k: ["sk-verysecretvalue1234567890abcdef"])
        assert tasks._adopt_landed_work(db, p, TARGET, "main", "") is True
        assert p.status == "awaiting_admin"
        assert p.dev_run_state == "failed"
        assert p.dev_run_error == "Adopted change blocked by leak scan"
        assert sent and "PUBLISHED change" in sent[0][1]
        msg = db.execute(select(Message).filter_by(project_id=p.id, author="agent")
                         ).scalars().one()
        assert "already visible" in msg.body


# ---- probe 2: PR/MR declared in the run's own pr.md on a connected repo ----

def _declared_setup(db, org_id, tmp_path, monkeypatch, *, created_at, state="open",
                    pr_repo="infra"):
    """The prod regression shape: run bound to the push repo, agent published its
    work as a PR on a connected CONTEXT repo and linked it in pr.md."""
    import datetime as dt

    p, req = _project(db, org_id, tmp_path)
    p.dev_run_started_at = dt.datetime(2026, 8, 12, 19, 37, tzinfo=dt.timezone.utc)
    db.add(ProjectRepo(project_id=p.id, ssh_uri="git@github.com:acme/app.git",
                       role="primary", provider="github", is_push_target=True))
    db.add(ProjectRepo(project_id=p.id, ssh_uri="git@github.com:acme/infra.git",
                       role="secondary", provider="github"))
    db.commit()
    ws = tmp_path / ".openvisor"
    ws.mkdir(exist_ok=True)
    (ws / "pr.md").write_text(
        "Bumped the proxy image.\n\n"
        f"PR: https://github.com/acme/{pr_repo}/pull/68\n")

    monkeypatch.setattr(tasks, "_project_repo_token", lambda db, pr, prov, uri=None: "tok")
    monkeypatch.setattr(
        tasks.github, "get_pr",
        lambda owner, repo, num, token=None: {
            "number": num, "state": state, "created_at": created_at,
            "head": {"ref": "f/#67-bump-proxy"},
            "html_url": f"https://github.com/{owner}/{repo}/pull/{num}"})
    monkeypatch.setattr(tasks, "_remote_ops",
                        lambda target, tok, br: {"diff": lambda n: "+image: 2.15.1\n"})
    from app.services import leakscan
    monkeypatch.setattr(leakscan, "platform_secret_values", lambda *a, **k: [])
    monkeypatch.setattr(leakscan, "kb_fingerprints_from_db", lambda *a, **k: [])
    return p, req


def test_no_changes_adopts_pr_declared_on_a_connected_context_repo(
        org_id, tmp_path, quiet, monkeypatch):
    """Prod regression (acme issue #67): the run was bound to the push repo,
    the agent judged the change belonged in a context repo, opened the PR itself
    and exited NO_CHANGES - the declared PR is adopted, pointers land on the repo
    the change actually lives on, and the park is awaiting_merge, not failed."""
    with SyncSession() as db:
        p, req = _declared_setup(db, org_id, tmp_path, monkeypatch,
                                 created_at="2026-08-12T19:42:30Z")
        assert tasks._adopt_declared_change(db, p, "main", "logs") is True
        assert p.dev_run_state == "awaiting_merge"
        assert p.dev_pr_number == 68
        assert p.dev_pr_url == "https://github.com/acme/infra/pull/68"
        assert p.dev_branch == "f/#67-bump-proxy"  # the PR's head, not the platform name
        assert any(r.get("url", "").endswith("/pull/68") for r in req.pr_urls)


def test_declared_change_ignores_referenced_and_foreign_prs(
        org_id, tmp_path, quiet, monkeypatch):
    with SyncSession() as db:
        # a PR merely CITED as reference (created before the run) never adopts
        p, _ = _declared_setup(db, org_id, tmp_path, monkeypatch,
                               created_at="2026-08-10T09:00:00Z")
        assert tasks._adopt_declared_change(db, p, "main", "") is False
        assert p.dev_run_state == "running"
    with SyncSession() as db:
        # a URL on a repo that is NOT connected to the project never adopts
        p2, _ = _declared_setup(db, org_id, tmp_path, monkeypatch,
                                created_at="2026-08-12T19:42:30Z",
                                pr_repo="someone-elses-repo")
        assert tasks._adopt_declared_change(db, p2, "main", "") is False
    with SyncSession() as db:
        # a closed PR never adopts
        p3, _ = _declared_setup(db, org_id, tmp_path, monkeypatch,
                                created_at="2026-08-12T19:42:30Z", state="closed")
        assert tasks._adopt_declared_change(db, p3, "main", "") is False


def test_declared_change_found_by_backticked_branch_and_pr_number(
        org_id, tmp_path, quiet, monkeypatch):
    """The ACTUAL prod regression summary shape: no URL - the pr.md says
    'branch `f/#67-…` pushed as PR #68'. The backticked head branch resolves the
    open PR on a connected repo and adopts it."""
    import datetime as dt

    with SyncSession() as db:
        p, req = _project(db, org_id, tmp_path)
        p.dev_run_started_at = dt.datetime(2026, 8, 12, 19, 37, tzinfo=dt.timezone.utc)
        db.add(ProjectRepo(project_id=p.id, ssh_uri="git@github.com:acme/app.git",
                           role="primary", provider="github", is_push_target=True))
        db.add(ProjectRepo(project_id=p.id, ssh_uri="git@github.com:acme/infra.git",
                           role="secondary", provider="github"))
        db.commit()
        ws = tmp_path / ".openvisor"
        ws.mkdir(exist_ok=True)
        (ws / "pr.md").write_text(
            "Bumps `jc21/nginx-proxy-manager` in `services/proxy/compose.yml`.\n\n"
            "Verified: branch `f/#67-bump-proxy` pushed as PR #68 against "
            "`master` (closes #67).\n")

        monkeypatch.setattr(tasks, "_project_repo_token", lambda db, pr, prov, uri=None: "tok")

        def fake_find_open_pr(owner, repo, head, token=None):
            if repo == "infra" and head == "f/#67-bump-proxy":
                return {"number": 68, "state": "open",
                        "created_at": "2026-08-12T19:42:30Z",
                        "head": {"ref": "f/#67-bump-proxy"},
                        "html_url": "https://github.com/acme/infra/pull/68"}
            return None
        monkeypatch.setattr(tasks.github, "find_open_pr", fake_find_open_pr)
        monkeypatch.setattr(tasks, "_remote_ops",
                            lambda target, tok, br: {"diff": lambda n: "+image: 2.15.1\n"})
        from app.services import leakscan
        monkeypatch.setattr(leakscan, "platform_secret_values", lambda *a, **k: [])
        monkeypatch.setattr(leakscan, "kb_fingerprints_from_db", lambda *a, **k: [])

        assert tasks._adopt_declared_change(db, p, "main", "logs") is True
        assert p.dev_pr_number == 68
        assert p.dev_pr_url == "https://github.com/acme/infra/pull/68"
        assert p.dev_branch == "f/#67-bump-proxy"
        assert p.dev_run_state == "awaiting_merge"


# ---- §14 resume-publish (probe 3, the no-changes path) ----------------------

def _resume_probe(monkeypatch, *, token="tok", branch=True, open_pr=False, ahead=True):
    monkeypatch.setattr(tasks, "_project_repo_token", lambda db, p, prov, uri=None: token)
    monkeypatch.setattr(tasks.github, "branch_exists",
                        lambda owner, repo, b, token=None: branch)
    monkeypatch.setattr(
        tasks.github, "find_open_pr",
        lambda owner, repo, head, token=None:
        {"number": 7, "html_url": "https://github.com/acme/app/pull/7"}
        if open_pr else None)
    monkeypatch.setattr(tasks.github, "branch_ahead_of_base",
                        lambda owner, repo, b, base, token=None: ahead)


def test_resume_publish_probe_matches_the_pushed_unopened_branch(
        org_id, tmp_path, quiet, monkeypatch):
    """The first live shared-repo engagement's shape: the branch holds the whole
    change from an earlier attempt, no PR exists, the resume verified and exited
    empty - the probe says publish, not fail."""
    with SyncSession() as db:
        p, _ = _project(db, org_id, tmp_path)
        _resume_probe(monkeypatch)
        assert tasks._resume_publishable_branch(db, p, TARGET) is True


def test_resume_publish_probe_declines_every_other_shape(
        org_id, tmp_path, quiet, monkeypatch):
    with SyncSession() as db:
        p, _ = _project(db, org_id, tmp_path)
        # no token to open a change with
        _resume_probe(monkeypatch, token=None)
        assert tasks._resume_publishable_branch(db, p, TARGET) is False
        # the branch never landed
        _resume_probe(monkeypatch, branch=False)
        assert tasks._resume_publishable_branch(db, p, TARGET) is False
        # an OPEN change on an empty run stays probe-1 territory: never relabel
        _resume_probe(monkeypatch, open_pr=True)
        assert tasks._resume_publishable_branch(db, p, TARGET) is False
        # branch equals base: a genuinely empty run
        _resume_probe(monkeypatch, ahead=False)
        assert tasks._resume_publishable_branch(db, p, TARGET) is False
        # no bound target / other host
        assert tasks._resume_publishable_branch(db, p, None) is False
        assert tasks._resume_publishable_branch(db, p, {"provider": "other"}) is False


def test_resume_publish_probe_swallows_provider_errors(
        org_id, tmp_path, quiet, monkeypatch):
    with SyncSession() as db:
        p, _ = _project(db, org_id, tmp_path)
        _resume_probe(monkeypatch)
        def boom(*a, **k):
            raise RuntimeError("api down")
        monkeypatch.setattr(tasks.github, "branch_exists", boom)
        assert tasks._resume_publishable_branch(db, p, TARGET) is False


def test_resume_publish_probe_gitlab_side(org_id, tmp_path, quiet, monkeypatch):
    with SyncSession() as db:
        p, _ = _project(db, org_id, tmp_path)
        target = {"provider": "gitlab", "base_url": "https://gl.example.com",
                  "path": "eng/e-1", "base_branch": "main", "customer": True}
        monkeypatch.setattr(tasks, "_project_repo_token",
                            lambda db, p, prov, uri=None: "tok")
        monkeypatch.setattr(tasks.gitlab, "customer_branch_exists",
                            lambda base, tok, path, b: True)
        monkeypatch.setattr(tasks.gitlab, "customer_find_open_mr",
                            lambda base, tok, path, b: None)
        monkeypatch.setattr(tasks.gitlab, "customer_branch_ahead",
                            lambda base, tok, path, b, bb: True)
        assert tasks._resume_publishable_branch(db, p, target) is True
        monkeypatch.setattr(tasks.gitlab, "customer_find_open_mr",
                            lambda base, tok, path, b: {"iid": 3})
        assert tasks._resume_publishable_branch(db, p, target) is False
