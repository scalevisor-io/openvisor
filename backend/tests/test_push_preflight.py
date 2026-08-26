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
                           workspace_path=None, gitlab_project_id=183)


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
                        lambda *a, **k: calls.heals.append((a, k)) or (heal() if callable(heal) else heal))
    monkeypatch.setattr(tasks.gitlab, "platform_deploy_key_id", lambda pid, key: 99)
    monkeypatch.setattr(tasks.gitlab, "is_platform_host", lambda uri: False)
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
                                ("ok", "")], token="glpat", heal="enabled")
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is True
    assert len(calls.probes) == 2 and not calls.parked
    (base_url, tok, path, title, key), kw = calls.heals[0]
    assert (base_url, tok, path) == ("https://gl.example", "glpat", "o/r")
    assert key == "ssh-ed25519 AAAA agent" and title.endswith(" agent")
    assert kw == {"key_id": 99}  # the platform repo's copy of the key object, to ENABLE it
    assert calls.msgs and "enabled the project's deploy key" in calls.msgs[0]
    assert "not allowed to push code" in calls.msgs[0]  # the customer learns why


def test_preflight_grants_the_installer_push_rights_on_the_platform_forge(monkeypatch):
    """GitLab authorises a deploy key's pushes as the account that installed it:
    on the platform's own forge that is the platform account, which a transfer
    out of the platform group strips of its membership - the key stays, the
    pushes stop. The heal gives it Developer access back through the customer's
    token, and says so."""
    calls = _wire(monkeypatch, [("denied", "GitLab: You are not allowed to push code to this project."),
                                ("ok", "")], token="glpat", heal="already installed")
    grants = []
    monkeypatch.setattr(tasks.gitlab, "is_platform_host", lambda uri: True)
    monkeypatch.setattr(tasks.gitlab, "platform_user", lambda: {"id": 17, "username": "openvisor-bot"})
    monkeypatch.setattr(tasks.gitlab, "customer_grant_member",
                        lambda base_url, token, path, user_id, access_level=30:
                        grants.append((path, user_id, access_level)) or "granted")
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is True
    assert grants == [("o/r", 17, 30)]
    assert "granted `openvisor-bot`" in calls.msgs[0] and "Developer access" in calls.msgs[0]


def test_preflight_parks_when_nothing_heals(monkeypatch):
    refusal = "GitLab: You are not allowed to push code to this project."
    calls = _wire(monkeypatch, [("denied", refusal)])  # no token: no heal possible
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is False
    assert calls.parked == [("awaiting_customer", "Repository refused the deploy key's push")]
    assert calls.saved[0][0] == "failed" and refusal in calls.saved[0][1]
    assert refusal in calls.msgs[0] and "account that installed it" in calls.msgs[0]
    assert "no GITLAB_TOKEN in the project Memory" in calls.msgs[0]  # why no heal ran
    assert "Nothing was built or billed" in calls.msgs[0]
    assert not calls.heals


def test_preflight_parks_with_the_apis_own_refusal_when_the_heal_fails(monkeypatch):
    """The heal's failure reason reaches the thread - a silent heal (prod
    2026-08-26: "fingerprint has already been taken") read as if nothing had
    been tried."""
    def boom():
        raise tasks.gitlab.GitLabError('deploy key install failed: HTTP 400 {"fingerprint":["has already been taken"]}')
    calls = _wire(monkeypatch, [("denied", "GitLab: The project you were looking for could not be found or you don't have permission to view it.")],
                  token="glpat", heal=boom)
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is False
    assert "I tried to fix the deploy key with your repository token, but deploy key install failed" in calls.msgs[0]
    assert "has already been taken" in calls.msgs[0]
    assert "doesn't know this project's deploy key" in calls.msgs[0]  # the hint for that refusal


def test_preflight_parks_when_the_heal_does_not_take(monkeypatch):
    calls = _wire(monkeypatch, [("denied", "refused"), ("denied", "still refused")],
                  token="glpat", heal="enabled")
    assert tasks._push_preflight(_Db(), _project(), _target(), "main") is False
    assert "after that the repository still answered: still refused" in calls.msgs[0] and calls.parked


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

def _gitlab_forge(monkeypatch, log, keys=None, instance_keys=None):
    """A GitLab that keeps ONE key object per fingerprint across the instance:
    `keys` is what /projects/<path>/deploy_keys lists for the project under
    test, `instance_keys` the fingerprints that exist anywhere (a POST of one
    of those answers the duplicate-fingerprint 400, /deploy_keys lists them
    when the token may read the instance)."""
    keys = list(keys or [])
    taken = set(instance_keys or [])

    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/v4/projects/berwick.ai%2Fcarouter.ai" or path == "/api/v4/projects/berwick.ai/carouter.ai":
            return httpx.Response(301, headers={"Location": "https://gl.example/api/v4/projects/182"})
        if path == "/api/v4/projects/182":
            return httpx.Response(200, json={"id": 182, "path_with_namespace": "flavienbwk/carouter.ai"})
        if path == "/api/v4/projects/same%2Fplace" or path == "/api/v4/projects/same/place":
            return httpx.Response(200, json={"id": 1, "path_with_namespace": "same/place"})
        if path == "/api/v4/deploy_keys":
            return (httpx.Response(200, json=[{"id": 99, "key": f"ssh-ed25519 {b} platform"} for b in taken])
                    if instance_keys is not None else httpx.Response(403))
        if path.endswith("/deploy_keys") and request.method == "GET":
            return httpx.Response(200, json=keys)
        if path.endswith("/deploy_keys") and request.method == "POST":
            body = json.loads(request.read())
            assert body["can_push"] is True
            if body["key"].split()[1] in taken:
                return httpx.Response(400, json={"message": {"deploy_key.fingerprint_sha256": ["has already been taken"]}})
            keys.append({"id": 9, "key": body["key"], "can_push": True})
            return httpx.Response(201, json={"id": 9})
        if path.endswith("/deploy_keys/99/enable") and request.method == "POST":
            keys.append({"id": 99, "key": "ssh-ed25519 AAAA platform", "can_push": False})
            return httpx.Response(201, json={"id": 99, "can_push": False})
        if "/deploy_keys/" in path and request.method == "PUT":
            kid = int(path.rsplit("/", 1)[1])
            for k in keys:
                if k["id"] == kid:
                    k["can_push"] = json.loads(request.read())["can_push"]
                    return httpx.Response(200, json=k)
            return httpx.Response(404)
        if path.endswith("/members") and request.method == "POST":
            body = json.loads(request.read())
            if body["user_id"] == 17:
                return httpx.Response(201, json={"id": 17, "access_level": body["access_level"]})
            return httpx.Response(409, json={"message": "Member already exists"})
        if "/deploy_keys/" in path and request.method == "DELETE":
            raise AssertionError("a deploy key must never be detached by the heal")
        return httpx.Response(404)
    monkeypatch.setattr(gitlab, "_customer_client", lambda base_url, token: httpx.Client(
        base_url="https://gl.example/api/v4", transport=httpx.MockTransport(handler)))
    return keys


def test_gitlab_resolve_moved_follows_one_redirect(monkeypatch):
    log = []
    _gitlab_forge(monkeypatch, log)
    assert gitlab.customer_resolve_moved("https://gl.example", "t", "berwick.ai/carouter.ai") == "flavienbwk/carouter.ai"
    assert gitlab.customer_resolve_moved("https://gl.example", "t", "same/place") is None
    assert gitlab.customer_resolve_moved("https://gl.example", "t", "gone/away") is None


KEY = "ssh-ed25519 AAAA agent\n"


def test_gitlab_ensure_deploy_key_installs_a_key_new_to_the_instance(monkeypatch):
    log = []
    keys = _gitlab_forge(monkeypatch, log, keys=[{"id": 8, "key": "ssh-ed25519 BBBB other", "can_push": False}])
    assert gitlab.customer_ensure_deploy_key("https://gl.example", "t", "flavienbwk/carouter.ai",
                                             "Openvisor agent", KEY) == "installed"
    # httpx hands the handler decoded paths (the wire carries %2F)
    assert ("POST", "/api/v4/projects/flavienbwk/carouter.ai/deploy_keys") in log
    assert [k["id"] for k in keys] == [8, 9]  # someone else's key stays
    with pytest.raises(gitlab.GitLabError):
        gitlab.customer_ensure_deploy_key("https://gl.example", "t", "x/y", "t", "")


def test_gitlab_ensure_deploy_key_enables_the_existing_key_object_instead_of_detaching(monkeypatch):
    """The prod regression: the key lived on the platform repo too, so a
    detach only unlinked it here and the re-add hit the duplicate-fingerprint
    400 - the repo was left with no key. Now the existing object is ENABLED
    (id from the platform repo, or the instance list) and given write access;
    no DELETE is ever issued."""
    log = []
    keys = _gitlab_forge(monkeypatch, log, keys=[], instance_keys=["AAAA"])
    assert gitlab.customer_ensure_deploy_key("https://gl.example", "t", "flavienbwk/carouter.ai",
                                             "Openvisor agent", KEY, key_id=99) == "enabled"
    assert ("POST", "/api/v4/projects/flavienbwk/carouter.ai/deploy_keys/99/enable") in log
    assert ("PUT", "/api/v4/projects/flavienbwk/carouter.ai/deploy_keys/99") in log
    assert keys == [{"id": 99, "key": "ssh-ed25519 AAAA platform", "can_push": True}]
    # without a known id the instance list answers (an admin token), else nothing changes
    log.clear()
    _gitlab_forge(monkeypatch, log, keys=[], instance_keys=["AAAA"])
    assert gitlab.customer_ensure_deploy_key("https://gl.example", "t", "flavienbwk/carouter.ai",
                                             "Openvisor agent", KEY) == "enabled"
    assert ("GET", "/api/v4/deploy_keys") in log
    log.clear()
    with pytest.raises(gitlab.GitLabError, match="cannot look it up"):
        _forge_taken_without_listing(monkeypatch, log)
    assert not any(m == "DELETE" for m, _ in log)


def _forge_taken_without_listing(monkeypatch, log):
    """A forge where the fingerprint is taken but /deploy_keys is not readable."""
    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.method, request.url.path))
        if request.url.path == "/api/v4/deploy_keys":
            return httpx.Response(403)
        if request.url.path.endswith("/deploy_keys") and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/deploy_keys") and request.method == "POST":
            return httpx.Response(400, json={"message": {"deploy_key.fingerprint_sha256": ["has already been taken"]}})
        return httpx.Response(404)
    monkeypatch.setattr(gitlab, "_customer_client", lambda base_url, token: httpx.Client(
        base_url="https://gl.example/api/v4", transport=httpx.MockTransport(handler)))
    return gitlab.customer_ensure_deploy_key("https://gl.example", "t", "x/y", "t", KEY)


def test_gitlab_ensure_deploy_key_grants_write_access_to_an_attached_key(monkeypatch):
    log = []
    keys = _gitlab_forge(monkeypatch, log, keys=[{"id": 7, "key": "ssh-ed25519 AAAA old", "can_push": False}])
    assert gitlab.customer_ensure_deploy_key("https://gl.example", "t", "x/y", "t", KEY) == "write access granted"
    assert keys[0]["can_push"] is True
    assert gitlab.customer_ensure_deploy_key("https://gl.example", "t", "x/y", "t", KEY) == "already installed"
    assert not any(m in ("POST", "DELETE") for m, _ in log)


def test_gitlab_grant_member_and_platform_user(monkeypatch):
    log = []
    _gitlab_forge(monkeypatch, log)
    assert gitlab.customer_grant_member("https://gl.example", "t", "flavienbwk/carouter.ai", 17) == "granted"
    assert gitlab.customer_grant_member("https://gl.example", "t", "flavienbwk/carouter.ai", 18) == "already a member"
    assert log.count(("POST", "/api/v4/projects/flavienbwk/carouter.ai/members")) == 2
    monkeypatch.setattr(gitlab, "_PLATFORM_USER", None)
    monkeypatch.setattr(gitlab, "_client", lambda: httpx.Client(
        base_url="https://gl.example/api/v4",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": 17, "username": "openvisor-bot"})
                                      if r.url.path == "/api/v4/user" else httpx.Response(404))))
    monkeypatch.setattr(gitlab.settings, "gitlab_url", "https://gl.example")
    monkeypatch.setattr(gitlab.settings, "gitlab_token", "platform")
    assert gitlab.platform_user() == {"id": 17, "username": "openvisor-bot"}


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
            return httpx.Response(200, json=list(keys.values()))
        if path.startswith("/repos/new/name/keys/") and request.method == "DELETE":
            keys.pop(int(path.rsplit("/", 1)[1]), None)
            return httpx.Response(204)
        if path == "/repos/new/name/keys" and request.method == "POST":
            body = json.loads(request.read())
            assert body["read_only"] is False
            if any(k["key"].split()[1] == body["key"].split()[1] for k in keys.values()) or body["key"].split()[1] in elsewhere:
                return httpx.Response(422, json={"message": "key is already in use"})
            keys[4] = {"id": 4, "key": body["key"], "read_only": False}
            return httpx.Response(201, json={"id": 4})
        return httpx.Response(404)
    keys = {3: {"id": 3, "key": "ssh-ed25519 AAAA x", "read_only": True}}
    elsewhere = {"CCCC"}  # a fingerprint some other repository owns
    monkeypatch.setattr(github, "_client", lambda token=None: httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    assert github.resolve_moved("old", "name", token="t") == ("new", "name")
    assert github.resolve_moved("same", "name", token="t") is None
    # a read-only copy on THIS repo: removed, re-added with write access
    assert github.ensure_deploy_key("new", "name", "Openvisor agent", "ssh-ed25519 AAAA agent", token="t") == "reinstalled"
    assert ("DELETE", "/repos/new/name/keys/3") in log and keys[4]["read_only"] is False
    assert github.ensure_deploy_key("new", "name", "Openvisor agent", "ssh-ed25519 AAAA agent", token="t") == "already installed"
    # a fresh key: one POST
    assert github.ensure_deploy_key("new", "name", "Openvisor agent", "ssh-ed25519 BBBB agent", token="t") == "installed"
    # a key some OTHER repository owns: reported, nothing removed
    n_deletes = sum(1 for m, _ in log if m == "DELETE")
    with pytest.raises(github.GitHubError, match="another GitHub repository"):
        github.ensure_deploy_key("new", "name", "Openvisor agent", "ssh-ed25519 CCCC agent", token="t")
    assert sum(1 for m, _ in log if m == "DELETE") == n_deletes


# ------------------------------------------------------------ runner contract

def test_runner_widens_the_sdk_retry_set_to_gateway_errors():
    if not RUNNER_DRIVER.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    src = RUNNER_DRIVER.read_text()
    assert "BadGatewayError" in src and "LLM_RETRY_EXCEPTIONS" in src
    assert src.index("LLM_RETRY_EXCEPTIONS") < src.index("llm = LLM(**llm_kwargs)")
