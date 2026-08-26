"""§push preflight (prod 2026-08-26): a build ran ten minutes and 3.2M tokens
into a two-line fix, then GitLab refused the push. The project had been
transferred out of the platform's group: its old path 301'd/405'd on every API
call, and its deploy key - installed by the platform account, which lost push
rights with the transfer - still READ (so the sandbox's fetch preflight passed)
and never wrote. Nothing asked "can this key push?" before building. Now the
worker asks first, with a hidden probe ref the remote's own hooks judge; a
moved repository is followed, a key whose installer lost their rights is
re-installed through the project's token, and only a refusal nobody can heal
parks - before a sandbox exists, with the remote's own words.
"""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.services import github, gitlab, repos
from app.workers import tasks

RUNNER_DRIVER = Path("/app/runner_src/run_dev.py")


def _env():
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _bare(tmp_path: Path, refuse: str | None = None) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    if refuse:
        hook = bare / "hooks" / "pre-receive"
        hook.write_text(f"#!/bin/sh\necho '{refuse}' >&2\nexit 1\n")
        hook.chmod(0o755)
    return bare


# ------------------------------------------------------------ the probe, on real git

def test_probe_push_leaves_nothing_behind_on_a_writable_remote(tmp_path):
    bare = _bare(tmp_path)
    proc = repos._probe_push(str(bare), tmp_path / "w", "refs/openvisor/preflight/run1", _env())
    assert proc.returncode == 0
    refs = subprocess.run(["git", "-C", str(bare), "show-ref"], capture_output=True, text=True)
    assert "preflight" not in refs.stdout  # pushed, judged, deleted


def test_probe_push_reports_the_remote_refusal_verbatim(tmp_path):
    bare = _bare(tmp_path, refuse="GitLab: You are not allowed to push code to this project.")
    proc = repos._probe_push(str(bare), tmp_path / "w", "refs/openvisor/preflight/run1", _env())
    assert proc.returncode != 0
    assert repos._push_cause(proc.stderr) == "GitLab: You are not allowed to push code to this project."


def test_push_cause_prefers_the_forge_over_gits_closing_advice():
    gitlab_err = ("remote: ========================================\n"
                  "remote: GitLab: You are not allowed to push code to this project.\n"
                  "To ssh://git.example:10022/o/r.git\n"
                  " ! [remote rejected] x -> x (pre-receive hook declined)\n"
                  "error: failed to push some refs to 'ssh://git.example:10022/o/r.git'")
    assert repos._push_cause(gitlab_err) == "GitLab: You are not allowed to push code to this project."
    github_err = ("ERROR: The key you are authenticating with has been marked as read only.\n"
                  "fatal: Could not read from remote repository.")
    assert repos._push_cause(github_err).startswith("ERROR: The key you are authenticating")
    ssh_err = ("Warning: Permanently added 'h' (ED25519) to the list of known hosts.\n"
               "git@h: Permission denied (publickey).\nfatal: Could not read from remote repository.")
    assert repos._push_cause(ssh_err) == "git@h: Permission denied (publickey)."
    assert repos._push_cause("") == "the remote answered nothing"


def test_check_push_needs_an_ssh_remote_and_a_key():
    assert repos.check_push("https://h/o/r.git", "k", "p")[0] == "error"
    assert repos.check_push("git@h:o/r.git", "", "p") == ("error", "no deploy key")


def test_check_push_classifies_the_probe(monkeypatch):
    def scripted(rc, stderr):
        return lambda *a, **k: subprocess.CompletedProcess(a, rc, "", stderr)
    monkeypatch.setattr(repos, "_probe_push", scripted(0, ""))
    assert repos.check_push("git@h:o/r.git", "k", "p")[0] == "ok"
    monkeypatch.setattr(repos, "_probe_push", scripted(1, "remote: GitLab: You are not allowed to push code to this project.\n"))
    assert repos.check_push("git@h:o/r.git", "k", "p") == (
        "denied", "GitLab: You are not allowed to push code to this project.")
    monkeypatch.setattr(repos, "_probe_push", scripted(128, "ssh: connect to host h port 22: Connection timed out\n"))
    verdict, detail = repos.check_push("git@h:o/r.git", "k", "p")
    assert verdict == "unreachable" and "Connection timed out" in detail
    # the probe ref is derived from the caller's id, sanitised
    seen = {}
    monkeypatch.setattr(repos, "_probe_push", lambda uri, repo, ref, env: seen.update(ref=ref) or subprocess.CompletedProcess([], 0, "", ""))
    repos.check_push("git@h:o/r.git", "k", "run/1 2")
    assert seen["ref"] == "refs/openvisor/preflight/run-1-2"


def test_replace_repo_path_keeps_the_transport_and_swaps_the_project():
    assert (repos.replace_repo_path("ssh://git@git.example:10022/berwick.ai/carouter.ai.git",
                                    "flavienbwk/carouter.ai")
            == "ssh://git@git.example:10022/flavienbwk/carouter.ai.git")
    assert repos.replace_repo_path("git@github.com:old/name.git", "new/name") == "git@github.com:new/name.git"
    assert repos.replace_repo_path("https://gitlab.com/old/name", "grp/sub/name") == "https://gitlab.com/grp/sub/name"
    assert repos.replace_repo_path("git@github.com:old/name.git", "") == "git@github.com:old/name.git"


def test_check_ssh_write_mode_reports_a_key_that_reads_but_cannot_push(monkeypatch):
    monkeypatch.setattr(repos.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(repos, "check_push",
                        lambda *a, **k: ("denied", "GitLab: You are not allowed to push code to this project."))
    ok, detail = repos.check_ssh("git@h:o/r.git", "key")
    assert ok and "has access" in detail  # read-only check unchanged
    ok, detail = repos.check_ssh("git@h:o/r.git", "key", write=True)
    assert not ok and "can't push" in detail and "not allowed to push code" in detail
    monkeypatch.setattr(repos, "check_push", lambda *a, **k: ("ok", ""))
    assert repos.check_ssh("git@h:o/r.git", "key", write=True) == (
        True, "Reachable, and the deploy key can push to this repository.")


# ------------------------------------------------------------ the copy

def test_push_failure_hint_names_the_lost_installer_and_the_wrong_repo():
    hint = tasks._push_failure_hint("remote: GitLab: You are not allowed to push code to this project.")
    assert "account that installed it" in hint and "moved or transferred" in hint
    hint = tasks._push_failure_hint("ERROR: Permission to o/r.git denied to deploy key")
    assert "one GitHub repository" in hint
    assert "Allow write access" in tasks._push_failure_hint("ERROR: The key ... marked as read only.")


# ------------------------------------------------------------ the worker preflight

def _project():
    return SimpleNamespace(id="p1", ssh_private_key_enc="enc", ssh_public_key="ssh-ed25519 AAAA agent",
                           git_author_name=None, git_author_email=None, dev_request_id=None,
                           workspace_path=None)


def _target(**over):
    t = {"repo_id": "r1", "remote": "ssh://git@git.example:10022/o/r.git", "provider": "gitlab",
         "customer": True, "base_url": "https://gl.example", "path": "o/r", "base_branch": "main"}
    t.update(over)
    return t


def _wire(monkeypatch, probes: list, token=None, heal=None):
    calls = SimpleNamespace(probes=[], msgs=[], parked=[], saved=[], heals=[])
    seq = iter(probes)
    monkeypatch.setattr(tasks, "decrypt", lambda v: "key")
    monkeypatch.setattr(tasks.dev_concurrency, "bound_run", lambda p: SimpleNamespace(id="run-abc-123456789"))
    monkeypatch.setattr(tasks.repolib, "check_push",
                        lambda remote, key, probe, author=None: calls.probes.append(probe) or next(seq))
    monkeypatch.setattr(tasks, "_project_repo_token", lambda db, p, prov, uri=None: token)
    monkeypatch.setattr(tasks.gitlab, "customer_ensure_deploy_key",
                        lambda *a, **k: calls.heals.append(a) or (heal() if callable(heal) else heal))
    monkeypatch.setattr(tasks, "_post_message", lambda db, pid, thread, author, body, **k: calls.msgs.append(body))
    monkeypatch.setattr(tasks, "_safe_transition", lambda db, p, to, reason: calls.parked.append((to, reason)))
    monkeypatch.setattr(tasks, "_save_run", lambda p, state, **k: calls.saved.append((state, k.get("error"))))
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    return calls


class _Db:
    def commit(self):
        pass


def test_preflight_builds_when_the_key_pushes(monkeypatch):
    calls = _wire(monkeypatch, [("ok", "")])
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is True
    assert calls.probes == ["run-abc-1234"] and not calls.msgs and not calls.parked


def test_preflight_heals_the_deploy_key_then_builds(monkeypatch):
    calls = _wire(monkeypatch, [("denied", "GitLab: You are not allowed to push code to this project."),
                                ("ok", "")], token="glpat", heal="reinstalled")
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is True
    assert len(calls.probes) == 2 and not calls.parked
    base_url, tok, path, title, key = calls.heals[0]
    assert (base_url, tok, path) == ("https://gl.example", "glpat", "o/r")
    assert key == "ssh-ed25519 AAAA agent" and title.endswith(" agent")
    assert calls.msgs and "reinstalled the project's deploy key" in calls.msgs[0]
    assert "not allowed to push code" in calls.msgs[0]  # the customer learns why


def test_preflight_parks_when_nothing_heals(monkeypatch):
    refusal = "GitLab: You are not allowed to push code to this project."
    calls = _wire(monkeypatch, [("denied", refusal)])  # no token: no heal possible
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is False
    assert calls.parked == [("awaiting_customer", "Repository refused the deploy key's push")]
    assert calls.saved[0][0] == "failed" and refusal in calls.saved[0][1]
    assert refusal in calls.msgs[0] and "account that installed it" in calls.msgs[0]
    assert "Nothing was built or billed" in calls.msgs[0]
    assert not calls.heals


def test_preflight_parks_when_the_heal_does_not_take(monkeypatch):
    calls = _wire(monkeypatch, [("denied", "refused"), ("denied", "still refused")],
                  token="glpat", heal="reinstalled")
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is False
    assert "still refused" in calls.msgs[0] and calls.parked


def test_preflight_is_not_a_verdict_on_the_network_or_the_platform_repo(monkeypatch):
    calls = _wire(monkeypatch, [("unreachable", "timed out")])
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is True  # the sandbox decides
    assert not calls.msgs
    calls = _wire(monkeypatch, [("ok", "")])
    assert tasks._push_preflight(_Db(), _project(), _target(repo_id=None), "main") is True
    assert not calls.probes  # platform repo: the platform key, no probe
    assert tasks._push_preflight(_Db(), _project(), _target(remote="https://h/o/r.git"), "main") is True
    assert not calls.probes


# ------------------------------------------------------------ the moved repository

def _row():
    return SimpleNamespace(id="r1", project_id="p1", provider="gitlab", role="primary",
                           ssh_uri="ssh://git@git.example:10022/berwick.ai/carouter.ai.git",
                           auto_merge=False, squash_on_merge=True, summarize_to_issue=False)


class _RowDb(_Db):
    def __init__(self, row):
        self.row, self.commits = row, 0

    def get(self, model, key):
        return self.row if key == self.row.id else None

    def commit(self):
        self.commits += 1


def test_heal_moved_repo_updates_the_row_and_rebuilds_the_target(monkeypatch):
    row, msgs = _row(), []
    monkeypatch.setattr(tasks, "_project_repo_token", lambda *a, **k: "glpat")
    monkeypatch.setattr(tasks.gitlab, "customer_resolve_moved",
                        lambda base_url, token, path: "flavienbwk/carouter.ai" if path == "berwick.ai/carouter.ai" else None)
    monkeypatch.setattr(tasks, "_post_message", lambda db, pid, thread, author, body, **k: msgs.append((thread, body)))
    monkeypatch.setattr(tasks, "_dev_thread", lambda db, p: "request:x")
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    db = _RowDb(row)
    target = tasks._repo_target(row)
    target["base_branch"] = "develop"
    healed = tasks._heal_moved_repo(db, _project(), target)
    assert row.ssh_uri == "ssh://git@git.example:10022/flavienbwk/carouter.ai.git"
    assert healed["remote"] == row.ssh_uri and healed["path"] == "flavienbwk/carouter.ai"
    assert healed["repo_id"] == "r1" and healed["base_branch"] == "develop"  # §repo binding intact
    assert db.commits == 1
    assert msgs[0][0] == "request:x" and "flavienbwk/carouter.ai" in msgs[0][1]


def test_heal_moved_repo_is_a_no_op_without_a_move(monkeypatch):
    row = _row()
    monkeypatch.setattr(tasks, "_project_repo_token", lambda *a, **k: "glpat")
    monkeypatch.setattr(tasks.gitlab, "customer_resolve_moved", lambda *a: None)
    target = tasks._repo_target(row)
    assert tasks._heal_moved_repo(_RowDb(row), _project(), target) is target
    # no token, no repo row, an `other` host: untouched
    monkeypatch.setattr(tasks, "_project_repo_token", lambda *a, **k: None)
    assert tasks._heal_moved_repo(_RowDb(row), _project(), target) is target
    assert tasks._heal_moved_repo(_RowDb(row), _project(), {"provider": "other", "repo_id": "r1"})["provider"] == "other"
    assert tasks._heal_moved_repo(_RowDb(row), _project(), None) is None


def test_poll_issues_follows_a_moved_repository_and_names_refusals(monkeypatch):
    def redirect(code):
        req = httpx.Request("GET", "https://gl.example/api/v4/projects/x/issues")
        return httpx.HTTPStatusError("redirect", request=req, response=httpx.Response(code, request=req))
    old, new = _target(), _target(path="new/r")
    seen = []

    def fetch(t):
        seen.append(t["path"])
        if t is old:
            raise redirect(301)
        return [{"iid": 1}]
    monkeypatch.setattr(tasks, "_heal_moved_repo", lambda db, p, t, thread=None: new)
    assert tasks._poll_issues(_Db(), _project(), old, fetch) == ([{"iid": 1}], None)
    assert seen == ["o/r", "new/r"]
    monkeypatch.setattr(tasks, "_heal_moved_repo", lambda db, p, t, thread=None: t)  # unresolved
    issues, why = tasks._poll_issues(_Db(), _project(), old, fetch)
    assert issues is None and "moved" in why

    def forbidden(t):
        raise redirect(403)
    issues, why = tasks._poll_issues(_Db(), _project(), old, forbidden)
    assert issues is None and "HTTP 403" in why


# ------------------------------------------------------------ the forge APIs

def _gitlab_forge(monkeypatch, log):
    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/v4/projects/berwick.ai%2Fcarouter.ai" or path == "/api/v4/projects/berwick.ai/carouter.ai":
            return httpx.Response(301, headers={"Location": "https://gl.example/api/v4/projects/182"})
        if path == "/api/v4/projects/182":
            return httpx.Response(200, json={"id": 182, "path_with_namespace": "flavienbwk/carouter.ai"})
        if path == "/api/v4/projects/same%2Fplace" or path == "/api/v4/projects/same/place":
            return httpx.Response(200, json={"id": 1, "path_with_namespace": "same/place"})
        if path.endswith("/deploy_keys") and request.method == "GET":
            return httpx.Response(200, json=[{"id": 7, "key": "ssh-ed25519 AAAA old-title", "can_push": True},
                                             {"id": 8, "key": "ssh-ed25519 BBBB other", "can_push": False}])
        if path.endswith("/deploy_keys/7") and request.method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/deploy_keys") and request.method == "POST":
            assert json.loads(request.read())["can_push"] is True
            return httpx.Response(201, json={"id": 9})
        return httpx.Response(404)
    monkeypatch.setattr(gitlab, "_customer_client", lambda base_url, token: httpx.Client(
        base_url="https://gl.example/api/v4", transport=httpx.MockTransport(handler)))


def test_gitlab_resolve_moved_follows_one_redirect(monkeypatch):
    log = []
    _gitlab_forge(monkeypatch, log)
    assert gitlab.customer_resolve_moved("https://gl.example", "t", "berwick.ai/carouter.ai") == "flavienbwk/carouter.ai"
    assert gitlab.customer_resolve_moved("https://gl.example", "t", "same/place") is None
    assert gitlab.customer_resolve_moved("https://gl.example", "t", "gone/away") is None


def test_gitlab_ensure_deploy_key_reinstalls_under_the_token(monkeypatch):
    log = []
    _gitlab_forge(monkeypatch, log)
    assert gitlab.customer_ensure_deploy_key("https://gl.example", "t", "flavienbwk/carouter.ai",
                                             "Openvisor agent", "ssh-ed25519 AAAA agent\n") == "reinstalled"
    # httpx hands the handler decoded paths (the wire carries %2F)
    assert ("DELETE", "/api/v4/projects/flavienbwk/carouter.ai/deploy_keys/7") in log
    assert ("POST", "/api/v4/projects/flavienbwk/carouter.ai/deploy_keys") in log
    assert not any(p.endswith("/deploy_keys/8") for _, p in log)  # someone else's key stays
    with pytest.raises(gitlab.GitLabError):
        gitlab.customer_ensure_deploy_key("https://gl.example", "t", "x/y", "t", "")


def test_github_resolve_moved_and_ensure_deploy_key(monkeypatch):
    log = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.method, request.url.path))
        path = request.url.path
        if path == "/repos/old/name":
            return httpx.Response(301, headers={"Location": "https://api.github.com/repositories/42"})
        if path == "/repositories/42":
            return httpx.Response(200, json={"full_name": "new/name"})
        if path == "/repos/same/name":
            return httpx.Response(200, json={"full_name": "same/name"})
        if path == "/repos/new/name/keys" and request.method == "GET":
            return httpx.Response(200, json=[{"id": 3, "key": "ssh-ed25519 AAAA x", "read_only": True}])
        if path == "/repos/new/name/keys/3" and request.method == "DELETE":
            return httpx.Response(204)
        if path == "/repos/new/name/keys" and request.method == "POST":
            assert json.loads(request.read())["read_only"] is False
            return httpx.Response(201, json={"id": 4})
        return httpx.Response(404)
    monkeypatch.setattr(github, "_client", lambda token=None: httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    assert github.resolve_moved("old", "name", token="t") == ("new", "name")
    assert github.resolve_moved("same", "name", token="t") is None
    assert github.ensure_deploy_key("new", "name", "Openvisor agent", "ssh-ed25519 AAAA agent", token="t") == "reinstalled"
    assert ("DELETE", "/repos/new/name/keys/3") in log


# ------------------------------------------------------------ runner contract

def test_runner_widens_the_sdk_retry_set_to_gateway_errors():
    if not RUNNER_DRIVER.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    src = RUNNER_DRIVER.read_text()
    assert "BadGatewayError" in src and "LLM_RETRY_EXCEPTIONS" in src
    assert src.index("LLM_RETRY_EXCEPTIONS") < src.index("llm = LLM(**llm_kwargs)")
