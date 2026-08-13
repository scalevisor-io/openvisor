"""§14.7 GitHub token resolution + AI security review + auto-merge / fix loop.

Covers: per-project GITHUB_TOKEN resolution (Memory beats platform env, absent →
None); the security-review deterministic floor (a planted secret/backdoor never
passes even if the model says pass, pass iff no critical/high) and its
local-heuristic / production-fail-closed behaviour; the no-token graceful path
(branch pushed → awaiting_merge, never the old hard fail) plus SSH merge
detection; and the auto-merge loop (clean → merge with the project token, a
critical/high finding → scoped fix re-dispatch, exhaustion → awaiting_customer
with the PR link, review error → fail closed, auto_merge off → manual park).
"""
import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.models import (
    CreditTransaction, Message, Organization, Project, ProjectMemory, ProjectRepo,
    StatusChange,
)
from app.services import events
from app.services.llm import LLMUnavailable
from app.workers import tasks
from app.workers.celery_app import celery

GH_REMOTE = "git@github.com:acme/widgets.git"
TARGET = {"provider": "github", "remote": GH_REMOTE, "owner": "acme",
          "repo": "widgets", "base_branch": "main", "auto_merge": False}


def _amerge(db, p, token, pr, target=None):
    """Drive the provider-agnostic auto-merge loop through the GitHub ops adapter
    (what _run_development_customer does once a PR is open with auto_merge on)."""
    tgt = target or TARGET
    ops = tasks._remote_ops(tgt, token)
    change = {"number": pr["number"], "url": pr.get("html_url")}
    tasks._remote_auto_merge(db, p, tgt, ops, change, "main", None, "logs")


@pytest.fixture
def quiet(monkeypatch):
    """Detach from redis/broker: capture WS events, swallow email + demo dispatch
    so _post_message / transition_sync run without external services."""
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(celery, "send_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: None)
    return ws


@pytest.fixture
def spoke_org():
    """Committed throwaway org, cleaned up with every row scoped to it (the tasks
    open their own sessions, so their target rows must be committed to be seen)."""
    with SyncSession() as db:
        org = Organization(name="Auto-merge Test Org", credit_balance=100.0)
        db.add(org)
        db.commit()
        oid = org.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(
                Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
                db.execute(delete(ProjectMemory).where(ProjectMemory.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _commit_project(oid, *, token_memory=None, auto_merge=False, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "running")
    kw.setdefault("ssh_private_key_enc", encrypt("PRIVATE-KEY-BODY"))
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.flush()
        # auto_merge lives on the push repo now (§multi-repo), not the project.
        db.add(ProjectRepo(project_id=p.id, ssh_uri=GH_REMOTE, role="primary",
                           provider="github", is_push_target=True, auto_merge=auto_merge))
        if token_memory is not None:
            db.add(ProjectMemory(project_id=p.id, author="customer", key="GITHUB_TOKEN",
                                 value_enc=encrypt(token_memory), is_secret=True))
        db.commit()
        return p.id


# ---------------------------------------------------------------- token resolution

def test_project_github_token_resolution(spoke_org, monkeypatch):
    pid = _commit_project(spoke_org)
    with SyncSession() as db:
        p = db.get(Project, pid)
        # nothing set anywhere → no token (the graceful no-token path)
        monkeypatch.setattr(tasks.settings, "github_token", "")
        assert tasks._project_github_token(db, p) is None
        # platform env is the fallback
        monkeypatch.setattr(tasks.settings, "github_token", "ghp_platform")
        assert tasks._project_github_token(db, p) == "ghp_platform"
    # a project GITHUB_TOKEN Memory secret wins over the platform env
    pid2 = _commit_project(spoke_org, token_memory="ghp_project")
    with SyncSession() as db:
        p2 = db.get(Project, pid2)
        monkeypatch.setattr(tasks.settings, "github_token", "ghp_platform")
        assert tasks._project_github_token(db, p2) == "ghp_project"


# ---------------------------------------------------------------- security review

def _proj_stub():
    from types import SimpleNamespace
    return SimpleNamespace(dev_request_id=None, id="p")


def _script_review(monkeypatch, result):
    from app.agents import pipeline
    monkeypatch.setattr(pipeline, "record_usage", lambda *a, **k: 0.0)
    monkeypatch.setattr(pipeline, "chat_json",
                        lambda *a, **k: (result, {"model": "m", "input_tokens": 1, "output_tokens": 1}))


def test_security_floor_scans_added_lines_only():
    from app.agents import pipeline
    # a secret only in context/removed lines, or in the +++ header, never trips
    diff = (" -----BEGIN RSA PRIVATE KEY-----\n"
            "-AKIAIOSFODNN7EXAMPLE\n"
            "+a harmless added line\n")
    assert pipeline.deterministic_security_findings(diff) == []
    assert pipeline.deterministic_security_findings("+++ b/keys.pem\n") == []


def test_security_review_floor_cannot_be_lowered(monkeypatch):
    from app.agents import pipeline
    # the model insists the PR is clean, but the diff commits a private key:
    # the deterministic floor forces a critical finding → never auto-mergeable
    _script_review(monkeypatch, {"verdict": "pass", "findings": []})
    diff = "+++ b/id_rsa\n+-----BEGIN RSA PRIVATE KEY-----\n+deadbeef\n"
    out = pipeline.run_security_review(None, _proj_stub(), diff)
    assert out["verdict"] == "changes_requested"
    assert "private-key-material" in out["floor"]
    assert any(f["severity"] == "critical" for f in out["findings"])


def test_security_review_pass_iff_no_critical_or_high(monkeypatch):
    from app.agents import pipeline
    clean = "+++ b/app.py\n+print('hello world')\n"
    # clean diff, model clean → pass
    _script_review(monkeypatch, {"verdict": "pass", "findings": []})
    assert pipeline.run_security_review(None, _proj_stub(), clean)["verdict"] == "pass"
    # model reports only a medium/low → still pass (verdict recomputed from severity,
    # never trusting the model's own verdict field)
    _script_review(monkeypatch, {"verdict": "changes_requested",
                                 "findings": [{"severity": "medium", "issue": "nit"}]})
    out = pipeline.run_security_review(None, _proj_stub(), clean)
    assert out["verdict"] == "pass"
    # a single high finding blocks
    _script_review(monkeypatch, {"verdict": "pass",
                                 "findings": [{"severity": "high", "issue": "SQLi",
                                               "file": "a.py", "line": 3}]})
    assert pipeline.run_security_review(None, _proj_stub(), clean)["verdict"] == "changes_requested"


def test_security_review_production_fails_closed_local_heuristic(monkeypatch):
    from app.agents import pipeline

    def boom(*a, **k):
        raise LLMUnavailable("endpoint down")

    monkeypatch.setattr(pipeline, "record_usage", lambda *a, **k: 0.0)
    monkeypatch.setattr(pipeline, "chat_json", boom)
    # production: an LLM outage propagates so the caller fails CLOSED
    monkeypatch.setattr(pipeline.settings, "deploy_env", "production")
    with pytest.raises(LLMUnavailable):
        pipeline.run_security_review(None, _proj_stub(), "+++ b/x\n+ok\n")
    # local: degrade to the deterministic floor only
    monkeypatch.setattr(pipeline.settings, "deploy_env", "local")
    assert pipeline.run_security_review(None, _proj_stub(), "+++ b/x\n+ok\n")["verdict"] == "pass"
    planted = "+++ b/x\n+AKIAIOSFODNN7EXAMPLE\n"
    assert pipeline.run_security_review(None, _proj_stub(), planted)["verdict"] == "changes_requested"


# ---- §Phase 1 #6: correctness findings are advisory, security still gates ----

def test_blocking_findings_rules():
    from app.agents import pipeline
    findings = [
        {"severity": "high", "category": "correctness", "issue": "logic bug"},     # advisory
        {"severity": "critical", "category": "correctness", "issue": "data loss"},  # critical always blocks
        {"severity": "medium", "category": "security", "issue": "weak"},            # medium never blocks
        {"severity": "high", "issue": "SQLi"},                                       # security high blocks
    ]
    issues = {f["issue"] for f in pipeline.blocking_findings(findings)}
    assert issues == {"data loss", "SQLi"}  # critical (any category) + high security


def test_critical_blocks_even_if_labeled_correctness(monkeypatch):
    from app.agents import pipeline
    clean = "+++ b/app.py\n+print('hi')\n"
    # the model (or a prompt-injection in the diff) tries to wave a critical security
    # issue through by tagging it 'correctness' - critical gates regardless of category
    _script_review(monkeypatch, {"verdict": "pass", "findings": [
        {"category": "correctness", "severity": "critical", "issue": "auth bypass mislabeled"}]})
    assert pipeline.run_security_review(None, _proj_stub(), clean)["verdict"] == "changes_requested"


def test_correctness_high_does_not_block_merge(monkeypatch):
    from app.agents import pipeline
    clean = "+++ b/app.py\n+print('hi')\n"
    # the model flags a HIGH correctness issue but no security issue → still auto-mergeable
    _script_review(monkeypatch, {"verdict": "changes_requested", "findings": [
        {"category": "correctness", "severity": "high",
         "issue": "the spec asked for CSV export but the diff never implements it"}]})
    out = pipeline.run_security_review(None, _proj_stub(), clean)
    assert out["verdict"] == "pass"  # correctness never gates
    # ...but the advisory finding IS surfaced/recorded for the customer + the eval
    assert any(f["category"] == "correctness" for f in out["findings"])


def test_security_high_still_blocks_even_with_correctness(monkeypatch):
    from app.agents import pipeline
    clean = "+++ b/app.py\n+print('hi')\n"
    _script_review(monkeypatch, {"verdict": "pass", "findings": [
        {"category": "correctness", "severity": "low", "issue": "minor gap"},
        {"category": "security", "severity": "high", "issue": "command injection"}]})
    assert pipeline.run_security_review(None, _proj_stub(), clean)["verdict"] == "changes_requested"


def test_normalize_findings_defaults_and_clamps_category():
    from app.agents.pipeline import _normalize_findings
    out = _normalize_findings([
        {"severity": "high", "issue": "a"},                         # no category -> security
        {"severity": "low", "category": "CORRECTNESS", "issue": "b"},  # normalized lower
        {"severity": "low", "category": "bogus", "issue": "c"},      # unknown -> security
    ])
    assert [f["category"] for f in out] == ["security", "correctness", "security"]


# ---------------------------------------------------------------- no-token path

def _mock_build_ok(monkeypatch):
    """Make the build+boot loop succeed deterministically without a real runner."""
    monkeypatch.setattr(tasks.settings, "openhands_enabled", False)
    monkeypatch.setattr(tasks, "_scaffold_placeholder", lambda p: None)
    monkeypatch.setattr(tasks, "_dispatch_runner",
                        lambda db, p, target, fix_instruction=None, **k: {"exit_code": "0", "logs": "built"})
    monkeypatch.setattr(tasks, "_verify_boot", lambda db, p: (True, ""))
    monkeypatch.setattr(tasks, "_bill_dev_run", lambda db, p: None)


def test_no_token_parks_awaiting_merge_never_fails(spoke_org, monkeypatch, quiet):
    pid = _commit_project(spoke_org)
    monkeypatch.setattr(tasks.settings, "github_token", "")
    _mock_build_ok(monkeypatch)
    opened = []
    monkeypatch.setattr(tasks.github, "open_pr", lambda *a, **k: opened.append(1) or {"number": 1})
    with SyncSession() as db:
        tasks._run_development_customer(db, db.get(Project, pid), TARGET, fix_only=False)
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"     # NOT "failed" (the old bug)
        assert p.status == "awaiting_customer"
        assert p.dev_pr_number is None                 # no PR without a token
    assert opened == []                                # never tried the API
    # the customer is told to open/merge the branch themselves
    assert any("branch" in ev["message"]["body"].lower()
               for _pid, ev in quiet if ev.get("type") == "message")


def test_ssh_merge_detection_deploys_no_token(spoke_org, monkeypatch, quiet):
    pid = _commit_project(spoke_org, dev_run_state="awaiting_merge",
                          status="awaiting_customer", dev_pr_number=None)
    monkeypatch.setattr(tasks.settings, "github_token", "")
    monkeypatch.setattr(tasks, "_agent_branch_merged_ssh", lambda pid_, remote: True)
    deployed = []
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: deployed.append(a))
    tasks.dev_pr_sweep()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
    assert deployed


def test_sweep_token_path_uses_api_not_ssh(spoke_org, monkeypatch, quiet):
    pid = _commit_project(spoke_org, token_memory="ghp_project",
                          dev_run_state="awaiting_merge", status="awaiting_customer",
                          dev_pr_number=7)
    monkeypatch.setattr(tasks.github, "get_pr",
                        lambda o, n, num, token=None: {"merged": True, "state": "open",
                                                       "base": {"ref": "main"}, "head": {"sha": "x"}})
    ssh_called = []
    monkeypatch.setattr(tasks, "_agent_branch_merged_ssh",
                        lambda *a: ssh_called.append(a) or True)
    deployed = []
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: deployed.append(a))
    tasks.dev_pr_sweep()
    assert ssh_called == []                            # token + PR number → API path only
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"
    assert deployed


# ---------------------------------------------------------------- auto-merge loop

def test_auto_merge_clean_merges_with_project_token(spoke_org, monkeypatch, quiet):
    pid = _commit_project(spoke_org, auto_merge=True, dev_pr_number=7)
    monkeypatch.setattr(tasks.github, "pr_diff", lambda *a, **k: "+clean\n")
    monkeypatch.setattr(tasks.pipeline, "run_security_review",
                        lambda db, p, diff: {"verdict": "pass", "findings": [], "floor": []})
    merged_tokens = []
    monkeypatch.setattr(tasks.github, "merge_pr",
                        lambda o, r, num, method="squash", token=None: merged_tokens.append(token) or (True, "merged"))
    deployed = []
    monkeypatch.setattr(tasks.demo_start, "apply_async", lambda *a, **k: deployed.append(a))
    with SyncSession() as db:
        p = db.get(Project, pid)
        _amerge(db, p, "ghp_project", {"number": 7, "html_url": "http://pr/7"})
    assert merged_tokens == ["ghp_project"]            # merged with the project token
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "deploying"
        assert p.dev_security_review["merged"] is True
    assert deployed


def test_auto_merge_fixes_then_merges(spoke_org, monkeypatch, quiet):
    pid = _commit_project(spoke_org, auto_merge=True, dev_pr_number=7)
    reviews = iter([
        {"verdict": "changes_requested",
         "findings": [{"severity": "high", "issue": "SQL injection", "file": "a.py", "line": 1}],
         "floor": []},
        {"verdict": "pass", "findings": [], "floor": []},
    ])
    monkeypatch.setattr(tasks.pipeline, "run_security_review", lambda db, p, diff: next(reviews))
    monkeypatch.setattr(tasks.github, "pr_diff", lambda *a, **k: "+q\n")
    dispatched = []
    monkeypatch.setattr(tasks, "_dispatch_runner",
                        lambda db, p, target, fix_instruction=None, **k: dispatched.append(fix_instruction) or {"exit_code": "0", "logs": "fixed"})
    monkeypatch.setattr(tasks, "_bill_dev_run", lambda db, p: None)
    monkeypatch.setattr(tasks, "_verify_boot", lambda db, p: (True, ""))
    monkeypatch.setattr(tasks.github, "merge_pr", lambda *a, **k: (True, "merged"))
    with SyncSession() as db:
        p = db.get(Project, pid)
        _amerge(db, p, "ghp_project", {"number": 7, "html_url": "http://pr/7"})
    assert len(dispatched) == 1                        # exactly one scoped fix run
    assert "SQL injection" in dispatched[0]            # findings fed to the fix
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "deploying"


def test_auto_merge_exhaustion_parks_with_pr_link(spoke_org, monkeypatch, quiet):
    pid = _commit_project(spoke_org, auto_merge=True, dev_pr_number=7)
    monkeypatch.setattr(tasks.settings, "security_fix_attempts", 2)
    monkeypatch.setattr(tasks.pipeline, "run_security_review",
                        lambda db, p, diff: {"verdict": "changes_requested",
                                             "findings": [{"severity": "critical", "issue": "backdoor",
                                                           "file": None, "line": None}],
                                             "floor": ["reverse-shell"]})
    monkeypatch.setattr(tasks.github, "pr_diff", lambda *a, **k: "+bad\n")
    dispatched = []
    monkeypatch.setattr(tasks, "_dispatch_runner",
                        lambda db, p, target, fix_instruction=None, **k: dispatched.append(1) or {"exit_code": "0", "logs": "x"})
    monkeypatch.setattr(tasks, "_bill_dev_run", lambda db, p: None)
    monkeypatch.setattr(tasks, "_verify_boot", lambda db, p: (True, ""))
    merged = []
    monkeypatch.setattr(tasks.github, "merge_pr", lambda *a, **k: merged.append(1) or (True, "merged"))
    with SyncSession() as db:
        p = db.get(Project, pid)
        _amerge(db, p, "ghp_project", {"number": 7, "html_url": "http://pr/7"})
    assert len(dispatched) == 2                         # exactly security_fix_attempts fixes
    assert merged == []                                 # never merged an unresolved PR
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"
        assert p.status == "awaiting_customer"
        assert p.dev_pr_number == 7                      # PR link retained for the customer


def test_auto_merge_review_error_fails_closed(spoke_org, monkeypatch, quiet):
    pid = _commit_project(spoke_org, auto_merge=True, dev_pr_number=7)
    monkeypatch.setattr(tasks.github, "pr_diff", lambda *a, **k: "+x\n")

    def boom(db, p, diff):
        raise LLMUnavailable("review endpoint down")

    monkeypatch.setattr(tasks.pipeline, "run_security_review", boom)
    merged = []
    monkeypatch.setattr(tasks.github, "merge_pr", lambda *a, **k: merged.append(1) or (True, "merged"))
    with SyncSession() as db:
        p = db.get(Project, pid)
        _amerge(db, p, "ghp_project", {"number": 7, "html_url": "http://pr/7"})
    assert merged == []                                 # a diff we couldn't review is never merged
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"
        assert p.status == "awaiting_customer"
        assert p.dev_security_review["verdict"] == "review_unavailable"


def test_token_without_auto_merge_opens_pr_and_parks(spoke_org, monkeypatch, quiet):
    pid = _commit_project(spoke_org, auto_merge=False)
    monkeypatch.setattr(tasks.settings, "github_token", "ghp_platform")
    _mock_build_ok(monkeypatch)
    monkeypatch.setattr(tasks.github, "ensure_base_branch", lambda *a, **k: None)
    monkeypatch.setattr(tasks.github, "open_pr",
                        lambda *a, **k: {"number": 11, "html_url": "http://pr/11"})
    reviewed = []
    monkeypatch.setattr(tasks.pipeline, "run_security_review",
                        lambda *a, **k: reviewed.append(1))
    with SyncSession() as db:
        tasks._run_development_customer(db, db.get(Project, pid), TARGET, fix_only=False)
    assert reviewed == []                               # no auto-merge review when the toggle is off
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "awaiting_merge"
        assert p.status == "awaiting_customer"
        assert p.dev_pr_number == 11
