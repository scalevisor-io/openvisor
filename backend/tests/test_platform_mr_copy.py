"""§PR copy hygiene (prod incident 2026-08-07): the entrypoint's fixed
merge_request.title push option retitled an agent-authored MR on every
recovery-dispatch push, and OpenHands' Co-authored-by commit trailer leaked
into the MR description. The worker-side personalize must never downgrade:
agent titles are kept, an existing description survives a pr.md-less dispatch,
and trailers are stripped from whatever ends up shown.
"""
from pathlib import Path
from types import SimpleNamespace

from app.core.db import SyncSession
from app.core.config import settings
from app.workers import tasks


def _project(tmp_path, branch="fix/slingshot-launcher-design"):
    return SimpleNamespace(id="p1", name="Angry Birds HTML Game",
                           dev_request_id=None, dev_branch=branch,
                           gitlab_project_id=1,
                           workspace_path=str(tmp_path))


def _write_pr_md(tmp_path, text):
    d = Path(tmp_path) / ".openvisor"
    d.mkdir(exist_ok=True)
    (d / "pr.md").write_text(text)


def test_strip_commit_trailers():
    body = ("Replace two-post pillars with Y-stem and pouch.\n\n"
            "Co-authored-by: openhands <openhands@all-hands.dev>\n"
            "Signed-off-by: bot <bot@x>\n")
    assert tasks._strip_commit_trailers(body) == (
        "Replace two-post pillars with Y-stem and pouch.")
    assert tasks._strip_commit_trailers(None) == ""


def test_is_generic_mr_title(tmp_path):
    p = _project(tmp_path)
    assert tasks._is_generic_mr_title(None, p)
    assert tasks._is_generic_mr_title("", p)
    assert tasks._is_generic_mr_title(f"{settings.brand_name} agent: MVP build", p)
    # GitLab's branch-derived defaults
    assert tasks._is_generic_mr_title("Fix/slingshot launcher design", p)
    assert tasks._is_generic_mr_title("Slingshot launcher design", p)
    # agent-authored: never downgrade
    assert not tasks._is_generic_mr_title(
        "Fix bird launcher to proper Y-shaped slingshot", p)


def _run_personalize(tmp_path, monkeypatch, current, pr_md=None):
    p = _project(tmp_path)
    if pr_md is not None:
        _write_pr_md(tmp_path, pr_md)
    calls: list = []
    monkeypatch.setattr(tasks.gitlab, "get_mr", lambda pid, iid: current)
    monkeypatch.setattr(tasks.gitlab, "update_mr",
                        lambda pid, iid, title=None, description=None:
                        calls.append({"title": title, "description": description}))
    # A session, because composing the copy also persists the run's summary
    # (§work answers); this throwaway project row is never in the DB.
    with SyncSession() as db:
        tasks._personalize_platform_mr(db, p, 7)
    return calls


def test_agent_title_kept_description_upgraded(tmp_path, monkeypatch):
    calls = _run_personalize(
        tmp_path, monkeypatch,
        {"title": "Fix bird launcher to proper Y-shaped slingshot",
         "description": "old\n\nCo-authored-by: openhands <o@a.dev>"},
        pr_md="Management summary of the slingshot rework.")
    assert calls == [{"title": None,
                      "description": "Management summary of the slingshot rework."}]


def test_generic_title_replaced_and_trailer_stripped_description_kept(tmp_path, monkeypatch):
    calls = _run_personalize(
        tmp_path, monkeypatch,
        {"title": f"{settings.brand_name} agent: MVP build",
         "description": "Replace pillars with Y-stem.\n\n"
                        "Co-authored-by: openhands <o@a.dev>"})
    assert len(calls) == 1
    assert calls[0]["title"] == f"{settings.brand_name}: Angry Birds HTML Game"
    assert calls[0]["description"] == "Replace pillars with Y-stem."


def test_noop_when_nothing_improves(tmp_path, monkeypatch):
    calls = _run_personalize(
        tmp_path, monkeypatch,
        {"title": "Fix bird launcher to proper Y-shaped slingshot",
         "description": "Replace pillars with Y-stem."})
    assert calls == []


def test_get_mr_failure_falls_back_to_plain_personalize(tmp_path, monkeypatch):
    p = _project(tmp_path)
    calls: list = []

    def boom(pid, iid):
        raise RuntimeError("gitlab down")

    monkeypatch.setattr(tasks.gitlab, "get_mr", boom)
    monkeypatch.setattr(tasks.gitlab, "update_mr",
                        lambda pid, iid, title=None, description=None:
                        calls.append({"title": title, "description": description}))
    tasks._personalize_platform_mr(None, p, 7)
    assert calls[0]["title"] == f"{settings.brand_name}: Angry Birds HTML Game"
    assert "Automated build" in calls[0]["description"]


# ---- commit-message twin (prod regression: PR #17 shipped a Co-authored-by:
# openhands trailer despite the project's no-attribution instruction - the
# agent's commit tooling appends it below any instruction's reach, so the
# entrypoint scrubs the unpushed range deterministically before pushing) ----

RUNNER_ENTRYPOINT = Path("/app/runner_src/entrypoint.sh")


def test_entrypoint_scrubs_attribution_trailers_before_push():
    if not RUNNER_ENTRYPOINT.exists():
        import pytest
        pytest.skip("runner source not mounted at /app/runner_src")
    src = RUNNER_ENTRYPOINT.read_text()
    scrub = src.index("filter-branch")
    assert "Co-authored-by|Signed-off-by" in src
    assert '"$SCRUB_BASE..HEAD"' in src  # bounded: never rewrites pushed/base history
    # the scrub sits between the leak scan and the push
    assert src.index("leak_scan.py") < scrub < src.index("PUSH_ARGS=")


def test_trailer_scrub_mechanism_rewrites_only_unpushed_range(tmp_path):
    """The exact commands the entrypoint runs, against a fixture repo: the
    unpushed agent commit loses its trailer (author and body kept), a clean
    commit and the pushed base history stay byte-identical."""
    import subprocess

    def git(cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout

    remote = tmp_path / "remote"
    remote.mkdir()
    git(tmp_path, "init", "--bare", str(remote))
    ws = tmp_path / "ws"
    ws.mkdir()
    git(tmp_path, "init", str(ws))
    git(ws, "config", "user.name", "acme")
    git(ws, "config", "user.email", "acme@example.com")
    git(ws, "remote", "add", "origin", str(remote))
    (ws / "f").write_text("base\n")
    git(ws, "add", "f")
    git(ws, "commit", "-m", "base\n\nCo-authored-by: legit human <h@x.y>")
    git(ws, "push", "-qu", "origin", "master")
    base_sha = git(ws, "rev-parse", "HEAD").strip()
    git(ws, "checkout", "-qb", "agent/work")
    (ws / "f").write_text("one\n")
    git(ws, "commit", "-am", "docs: change\n\nBody.\n\nCo-authored-by: openhands <openhands@all-hands.dev>")
    (ws / "f").write_text("two\n")
    git(ws, "commit", "-am", "clean follow-up")
    clean_sha = git(ws, "rev-parse", "HEAD").strip()

    scrub_base = git(ws, "merge-base", "origin/master", "HEAD").strip()
    subprocess.run(
        ["git", "-C", str(ws), "filter-branch", "-f", "--msg-filter",
         "sed -E '/^[[:space:]]*(Co-authored-by|Signed-off-by):/Id'",
         "--", f"{scrub_base}..HEAD"],
        check=True, capture_output=True,
        env={"FILTER_BRANCH_SQUELCH_WARNING": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"})

    log = git(ws, "log", "--format=%an|%s|%b---")
    assert "openhands" not in log
    assert "Body." in log  # trailer removed, body kept
    assert "legit human" in log  # base history untouched...
    assert git(ws, "rev-parse", f"{scrub_base}").strip() == base_sha  # ...same sha
    assert git(ws, "rev-parse", "HEAD").strip() != clean_sha  # range rewritten
    assert log.count("acme|") == 3  # authorship preserved everywhere
