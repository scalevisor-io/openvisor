"""§ssh remotes (prod 2026-08-13): a connected repo spelled `git@host:10022/grp/r.git`
silently dialled port 22 (git's scp-like syntax has no port field - 10022 became
the first PATH segment), and the repo card's Verify SSH reported an unreachable
host for a correctly installed deploy key because the API pod, like the worker,
has no hostAlias for a tailnet-only git host. Both are transport, so both live in
services/repos.py now: one canonical spelling, one host rewrite.
"""
from app.core.config import settings
from app.services import repos
from app.workers import tasks


# ------------------------------------------------------------ canonical form

def test_scp_style_with_a_port_becomes_a_url():
    """The shape everyone writes after reading 'SSH port 10022' off a self-hosted
    GitLab - and the one git misreads as a path."""
    assert (repos.normalize_ssh_uri("git@git.example.com:10022/grp/repo.git")
            == "ssh://git@git.example.com:10022/grp/repo.git")
    assert repos.normalize_ssh_uri("  git@h:2222/a/b.git  ") == "ssh://git@h:2222/a/b.git"


def test_valid_remotes_are_left_exactly_as_written():
    for uri in ("git@github.com:org/repo.git",           # plain scp-like: valid
                "ssh://git@git.example.com:10022/grp/repo.git",
                "https://gitlab.example.com/grp/repo.git",
                "git@host:10022",                        # no path - not the shape
                "git@host:v2/repo.git",                  # non-numeric first segment
                ""):
        assert repos.normalize_ssh_uri(uri) == uri.strip()
    assert repos.normalize_ssh_uri(None) == ""


# ------------------------------------------------------------ host rewrite

def test_rewrite_routes_both_remote_spellings(monkeypatch):
    monkeypatch.setattr(settings, "git_extra_host", "git.example.com:gitlab-ssh.svc")
    assert repos.git_host_rewrite("ssh://git@git.example.com:10022/g/r.git") == [
        "-c", ("url.ssh://git@gitlab-ssh.svc:10022/"
               ".insteadOf=ssh://git@git.example.com:10022/")]
    # scp-like: the path is home-relative, which the ssh:// form spells with /
    assert repos.git_host_rewrite("git@git.example.com:g/r.git") == [
        "-c", "url.ssh://git@gitlab-ssh.svc/.insteadOf=git@git.example.com:"]


def test_rewrite_is_a_noop_off_the_mapped_host(monkeypatch):
    monkeypatch.setattr(settings, "git_extra_host", "git.example.com:gitlab-ssh.svc")
    assert repos.git_host_rewrite("ssh://git@github.com/org/repo.git") == []
    assert repos.git_host_rewrite("git@github.com:org/repo.git") == []
    assert repos.git_host_rewrite("https://git.example.com/g/r.git") == []
    monkeypatch.setattr(settings, "git_extra_host", "")
    assert repos.git_host_rewrite("ssh://git@git.example.com:10022/g/r.git") == []


def test_the_worker_uses_the_same_resolver():
    """One resolver: the worker's transport and the API's checks must not drift."""
    assert "git_host_rewrite" in tasks.repolib.__dict__


# ------------------------------------------------------------ the SSH check

def _fake_git(monkeypatch, rc=0, stderr=""):
    seen: dict = {}

    class R:
        returncode = rc

        def __init__(self):
            self.stdout, self.stderr = "", stderr

    def fake_run(args, **kw):
        seen["args"] = args
        return R()

    monkeypatch.setattr(repos.subprocess, "run", fake_run)
    return seen


KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----"


def test_check_ssh_canonicalises_and_routes_the_transport(monkeypatch):
    monkeypatch.setattr(settings, "git_extra_host", "git.example.com:gitlab-ssh.svc")
    seen = _fake_git(monkeypatch)
    ok, detail = repos.check_ssh("git@git.example.com:10022/grp/repo.git", KEY)
    assert ok and "deploy key has access" in detail
    args = seen["args"]
    assert args[:2] == ["git", "-c"] and "gitlab-ssh.svc:10022" in args[2]
    assert args[-1] == "ssh://git@git.example.com:10022/grp/repo.git"  # canonical


def test_check_ssh_quotes_ssh_not_gits_closing_advice(monkeypatch):
    """git's 'make sure you have the correct access rights and the repository
    exists' reads like auth for a host that never answered - and it is last."""
    _fake_git(monkeypatch, rc=128, stderr=(
        "ssh: connect to host git.example.com port 10022: Connection timed out\n"
        "fatal: Could not read from remote repository.\n\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"))
    ok, detail = repos.check_ssh("ssh://git@git.example.com:10022/g/r.git", KEY)
    assert not ok
    assert "Connection timed out" in detail and "Couldn't reach the host" in detail
    assert "repository exists" not in detail


def test_check_ssh_still_names_an_unauthorized_key(monkeypatch):
    _fake_git(monkeypatch, rc=128, stderr=(
        "git@git.example.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository.\n"))
    ok, detail = repos.check_ssh("ssh://git@git.example.com:10022/g/r.git", KEY)
    assert not ok and "deploy key" in detail


def test_default_branch_detection_routes_the_transport_too(monkeypatch):
    monkeypatch.setattr(settings, "git_extra_host", "git.example.com:gitlab-ssh.svc")
    seen = _fake_git(monkeypatch, rc=0)
    repos.detect_default_branch("git@git.example.com:10022/g/r.git", KEY)
    assert "gitlab-ssh.svc" in seen["args"][2]
    assert "ssh://git@git.example.com:10022/g/r.git" in seen["args"]
