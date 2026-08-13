"""§build panel branch chip: the serialized branch web-URL derivation and the
stale-PR-pointer clear when a fresh branch is named (a pointer left by an
earlier work unit must never read as the current run's PR).
"""
from types import SimpleNamespace

from app.api import serializers
from app.core.db import SyncSession
from app.models import Organization, Project
from app.workers import tasks


def _p(branch="agent/mvp", repos=(), web_url=None, state="awaiting_merge"):
    # awaiting_merge: the URL-derivation tests below need a state where the
    # branch link is served at all (see test_branch_links_only_while_it_exists)
    return SimpleNamespace(dev_branch=branch, repos=list(repos),
                           gitlab_web_url=web_url, dev_run_state=state)


def test_branch_links_only_while_it_exists():
    """The tree link is served ONLY in states where the branch verifiably
    exists on the remote: mid-run it usually isn't pushed yet, and a platform
    merge deletes it - a dead chip link 404'd from the console (prod
    2026-08-12). Everywhere else the chip shows the name unlinked."""
    p = _p(repos=[_repo("git@github.com:acme/app.git", "github")])
    for state in ("idle", "running", "deploying", "merged", "failed", "done"):
        p.dev_run_state = state
        assert serializers.branch_url(p) is None, state
    for state in ("awaiting_merge", "superseded"):
        p.dev_run_state = state
        assert serializers.branch_url(p) == "https://github.com/acme/app/tree/agent/mvp"


def _repo(ssh_uri, provider, push=True):
    return SimpleNamespace(ssh_uri=ssh_uri, provider=provider, is_push_target=push)


def test_branch_url_github_and_gitlab_forms():
    p = _p(repos=[_repo("git@github.com:acme/app.git", "github")])
    assert serializers.branch_url(p) == "https://github.com/acme/app/tree/agent/mvp"
    p = _p(repos=[_repo("ssh://git@gitlab.example.com:2222/grp/app.git", "gitlab")])
    assert serializers.branch_url(p) == "https://gitlab.example.com/grp/app/-/tree/agent/mvp"


def test_branch_url_platform_other_and_missing():
    # platform GitLab fallback when no customer push repo
    p = _p(web_url="https://gitlab.example.com/plat/proj")
    assert serializers.branch_url(p) == "https://gitlab.example.com/plat/proj/-/tree/agent/mvp"
    # 'other' host: name shown unlinked
    p = _p(repos=[_repo("git@sr.ht:~x/app", "other")])
    assert serializers.branch_url(p) is None
    # no branch yet / nothing to link
    assert serializers.branch_url(_p(branch=None, web_url="https://g/x")) is None
    assert serializers.branch_url(_p()) is None


def test_fresh_branch_clears_stale_pr_pointer(monkeypatch):
    monkeypatch.setattr(tasks.events, "publish_sync", lambda *a, **k: None)
    monkeypatch.setattr(tasks.pipeline, "generate_branch_name",
                        lambda *a, **k: "agent/new-work")
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, project: ("", "k", "m"))
    with SyncSession() as db:
        org = Organization(name="BranchChip Test Org")
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai",
                    status="development", dev_pr_number=7,
                    dev_pr_url="https://github.com/acme/app/pull/7")
        db.add(p)
        db.flush()
        try:
            # _ensure_dev_branch commits internally, so clean up explicitly
            tasks._ensure_dev_branch(db, p)
            assert p.dev_branch == "agent/new-work"
            assert p.dev_pr_number is None and p.dev_pr_url is None
            # resume continuity: an assigned branch keeps its (current) PR pointer
            p.dev_pr_number = 8
            p.dev_pr_url = "https://github.com/acme/app/pull/8"
            tasks._ensure_dev_branch(db, p)
            assert p.dev_branch == "agent/new-work"
            assert p.dev_pr_number == 8
        finally:
            db.delete(p)
            db.flush()
            db.delete(org)
            db.commit()


def test_branch_url_pins_to_the_change_url_over_the_current_target():
    """Switching the working repo must not re-link history (prod regression):
    a stored PR/MR web URL names the repo the change actually lives on and
    wins over the CURRENT push target."""
    p = _p(branch="feat/upgrade-tracelib-v4",
           repos=[_repo("git@github.com:acme/Storefront.git", "github")])
    p.dev_pr_url = "https://github.com/acme/acme-infrastructure/pull/66"
    assert serializers.branch_url(p) == \
        "https://github.com/acme/acme-infrastructure/tree/feat/upgrade-tracelib-v4"
    # gitlab MR form, both /-/ and legacy paths
    p.dev_pr_url = "https://gitlab.example.com/grp/app/-/merge_requests/5"
    assert serializers.branch_url(p) == \
        "https://gitlab.example.com/grp/app/-/tree/feat/upgrade-tracelib-v4"
    # no PR yet -> today's behavior: the current push repo
    p.dev_pr_url = None
    assert serializers.branch_url(p) == \
        "https://github.com/acme/Storefront/tree/feat/upgrade-tracelib-v4"


def test_dev_run_out_branch_link_survives_a_target_switch():
    """The run-history rows carry their own pr_url - their branch links stay
    on THEIR repo whatever the project's push target says today."""
    row = SimpleNamespace(id="r1", request_id=None, repo_id=None,
                          state="awaiting_merge",
                          created_at=None, started_at=None,
                          branch="feat/upgrade-tracelib-v4", pr_number=66,
                          pr_url="https://github.com/acme/acme-infrastructure/pull/66",
                          run_error=None, security_review=None,
                          tokens_consumed=0, cost_credits=0.0, workspace_dir="")
    proj = SimpleNamespace(repos=[_repo("git@github.com:acme/Storefront.git",
                                        "github")],
                           gitlab_web_url=None)
    out = serializers.dev_run_out(row, proj, legacy_feed_owner=None)
    assert out["branch_url"] == \
        "https://github.com/acme/acme-infrastructure/tree/feat/upgrade-tracelib-v4"


def test_branch_url_percent_encodes_fragment_chars():
    """A KB-convention branch like `f/#67-…` must encode `#` (%23) or the browser
    truncates the URL at the fragment delimiter and the tree link 404s (prod
    2026-08-12). `/` stays literal - both providers take multi-segment paths."""
    b = "f/#67-bump-nginx-proxy-manager-to-2.15.1"
    p = _p(branch=b, repos=[_repo("git@github.com:acme/acme-infrastructure.git", "github")])
    assert serializers.branch_url(p) == (
        "https://github.com/acme/acme-infrastructure/tree/"
        "f/%2367-bump-nginx-proxy-manager-to-2.15.1")
    p = _p(branch=b, repos=[_repo("git@gitlab.example.com:grp/app.git", "gitlab")])
    assert serializers.branch_url(p).endswith("/-/tree/f/%2367-bump-nginx-proxy-manager-to-2.15.1")
    # pinned-URL path encodes too
    pinned = SimpleNamespace(dev_branch=b, repos=[],
                             dev_pr_url="https://github.com/acme/acme-infrastructure/pull/68",
                             gitlab_web_url=None, dev_run_state="awaiting_merge")
    assert serializers.branch_url(pinned).endswith("/tree/f/%2367-bump-nginx-proxy-manager-to-2.15.1")
