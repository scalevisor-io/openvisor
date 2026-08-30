"""§sandbox git preflight (prod incident 2026-08-13): a dev sandbox that cannot
reach the git remote used to WARN and build anyway - a full agent session whose
work could never be pushed. The runner now proves the remote answers before the
agent starts, and the dispatcher re-rolls that verdict into a fresh sandbox
(each Job carries a new network identity) instead of blaming the build.
"""
from pathlib import Path
from types import SimpleNamespace

from app.models import Project
from app.workers import tasks

RUNNER_ENTRYPOINT = Path("/app/runner_src/entrypoint.sh")

UNREACHABLE = {"exit_code": "6",
               "logs": "runner: GIT_REMOTE_UNREACHABLE - no route from this sandbox\n"}
DENIED = {"exit_code": "6",
          "logs": "runner: GIT_REMOTE_DENIED - the git host answered but refused\n"}


def _dispatch(monkeypatch, results: list[dict]) -> list[dict]:
    """Run _dispatch_runner against a scripted sequence of runner outcomes and
    return the calls the deployer client received."""
    calls: list[dict] = []

    def fake_run(*a, **kw):
        calls.append(kw)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(tasks, "_project_model_config", lambda db, p: ("http://llm", "k", "m"))
    monkeypatch.setattr(tasks, "_prepare_runner_inputs", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_project_reasoning_effort", lambda db, p: "")
    monkeypatch.setattr(tasks.dev_concurrency, "bound_run", lambda p: None)
    monkeypatch.setattr(tasks.egress, "is_enabled", lambda db: False)
    # §dev harness: resolved from the db like the egress flag above, and these
    # dispatch tests run without one - pin the default driver.
    monkeypatch.setattr(tasks.dev_harness, "resolve",
                        lambda db, p: tasks.dev_harness.HARNESSES["openhands"])
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(tasks.deployer_client, "run_dev_job", fake_run)

    project = Project(org_id="o", name="P", description="d", kind="ai")
    target = {"provider": "gitlab", "remote": "git@git.example:o/r.git",
              "base_branch": "main", "runner_provider": "gitlab"}
    out = tasks._dispatch_runner(None, project, target)
    return calls, out


# ------------------------------------------------------------ the verdicts

def test_remote_verdicts_need_both_the_code_and_the_sentinel():
    assert tasks._remote_unreachable(UNREACHABLE)
    assert tasks._remote_denied(DENIED)
    # the sentinels never cross-talk: a refusal is the customer's to fix
    assert not tasks._remote_denied(UNREACHABLE)
    assert not tasks._remote_unreachable(DENIED)
    # a log line alone is not a verdict (the runner echoes git's own output)
    assert not tasks._remote_unreachable({"exit_code": "1", "logs": "GIT_REMOTE_UNREACHABLE"})
    assert not tasks._remote_unreachable({"exit_code": "6", "logs": "boom"})
    assert not tasks._remote_unreachable(None)


# ------------------------------------------------------------ the re-roll

def test_dispatch_re_rolls_an_unreachable_sandbox(monkeypatch):
    calls, out = _dispatch(monkeypatch, [UNREACHABLE, UNREACHABLE, {"exit_code": "0"}])
    assert len(calls) == 3 and out["exit_code"] == "0"


def test_dispatch_stops_at_the_attempt_budget(monkeypatch):
    calls, out = _dispatch(monkeypatch, [UNREACHABLE])
    assert len(calls) == tasks._GIT_PREFLIGHT_ATTEMPTS
    assert tasks._remote_unreachable(out)  # the verdict reaches the park copy


def test_dispatch_never_re_rolls_a_real_failure(monkeypatch):
    """Only the preflight verdict is retryable: a build that ran and failed must
    reach the customer once, not three times."""
    for result in ({"exit_code": "1", "logs": "agent crashed"}, DENIED,
                   {"exit_code": "5", "logs": "NO_CHANGES_TO_PUBLISH"}):
        calls, out = _dispatch(monkeypatch, [result])
        assert len(calls) == 1 and out is result


# ------------------------------------------------------------ the park copy

def test_exit_copy_names_the_infrastructure_fault(tmp_path, monkeypatch):
    feed: list = []
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: feed.append(a))
    p = SimpleNamespace(id="p1", workspace_path=str(tmp_path), dev_request_id=None)

    chat, err = tasks._runner_exit_copy(p, UNREACHABLE)
    assert "couldn't reach your code repository" in chat and "Resume" in chat
    assert "before spending anything" in chat  # the customer is not billed for it
    assert "reach the git remote" in err
    assert feed and feed[-1][1] == "error"

    chat, err = tasks._runner_exit_copy(p, DENIED)
    assert "deploy key" in chat and "write access" in chat
    assert "deploy key" in err

    # no verdict → the pre-existing generic copy, unchanged
    chat, err = tasks._runner_exit_copy(p, {"exit_code": "1"})
    assert err == "" and "exited with an error" in chat


# ------------------------------------------------------------ runner contract

def test_entrypoint_proves_the_remote_before_building():
    if not RUNNER_ENTRYPOINT.exists():
        import pytest
        pytest.skip("runner source not mounted at /app/runner_src")
    src = RUNNER_ENTRYPOINT.read_text()
    pre = src.index("ls-remote origin")
    assert src.index("GIT_REMOTE_UNREACHABLE") > pre  # verdict follows the probe
    assert "GIT_REMOTE_DENIED" in src
    assert src.index("exit 6") < src.index("starting headless build")
    # an empty/new remote answers ls-remote and must still build
    assert 'REMOTE_ERR="reachable"' in src
    # a run that publishes nothing keeps exploring on the seeded workspace
    assert 'elif [[ "${GIT_PUSH:-0}" != "1" ]]' in src
    # the probe bounds its own connect so the verdict comes with git's real
    # error, and an empty capture still reads as something
    assert "ConnectTimeout=20" in src
    assert "${REMOTE_DETAIL:-" in src
    # ssh's cause is the first line; git's generic access-rights advice is the
    # last one and reads like auth even when the host never answered
    assert '"$REMOTE_ERR" | head -n 2' in src
