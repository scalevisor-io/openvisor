"""Repo-management policy shared by the projects API and the dev worker: provider
detection from a URL, the per-provider auth check, and the auto-merge token key.
github.py / gitlab.py hold the transport; this is the thin policy layer on top."""
import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from app.core.config import settings
from app.services import github, gitlab

# Providers whose PR/MR flow we can auto-merge. "other" repos are still buildable
# (branch pushed over the deploy key, customer merges) but never auto-merged.
AUTO_MERGE_PROVIDERS = ("github", "gitlab")


def detect_provider(uri: str | None) -> str:
    """github | gitlab | other from the repo URL host. 'other' = a host with no
    PR/MR API integration; the UI disables auto-merge and asks the user to pick a
    platform when it's actually a self-hosted GitHub/GitLab on an odd domain."""
    if github.is_github(uri):
        return "github"
    if gitlab.is_gitlab(uri):
        return "gitlab"
    return "other"


def token_key(provider: str) -> str | None:
    """The Memory secret name whose PAT authenticates auto-merge for this provider."""
    return {"github": "GITHUB_TOKEN", "gitlab": "GITLAB_TOKEN"}.get(provider)


def default_git_identity() -> tuple[str, str]:
    """(name, email) the agent commits as when a project sets no override. Derived
    from the brand so a white-label spoke never signs commits with another brand's
    identity."""
    return f"{settings.brand_name} agent", f"agent@{settings.deploy_domain}"


def git_identity(project) -> tuple[str, str]:
    """(name, email) for THIS project's commits: the per-project override where
    set, else the instance default. The single resolution point - the worker, the
    API payload and the UI hint all read it, so they can never disagree."""
    name, email = default_git_identity()
    return ((project.git_author_name or "").strip() or name,
            (project.git_author_email or "").strip() or email)


def check_auth(provider: str, ssh_uri: str, token: str) -> tuple[bool, str]:
    """Authenticated repo-access check gating the auto-merge toggle: a real API
    call confirming the token is valid AND can reach THAT repo. Returns
    (ok, detail). Only github/gitlab are checkable."""
    if provider == "github":
        try:
            owner, repo = github.parse_repo(ssh_uri)
        except github.GitHubError as exc:
            return False, str(exc)
        return github.check_repo_access(owner, repo, token)
    if provider == "gitlab":
        try:
            base_url = gitlab.customer_base_url(ssh_uri)
            path = gitlab.parse_repo_path(ssh_uri)
        except gitlab.GitLabError as exc:
            return False, str(exc)
        return gitlab.check_repo_access(base_url, path, token)
    return False, "Auto-merge is only available for GitHub or GitLab repositories."


def is_ssh_uri(uri: str | None) -> bool:
    """True for a git SSH remote the customer authenticates with the deploy key:
    an `ssh://…` URL or the scp-like `git@host:path`. https:// / http:// remotes
    (and the platform repo, which the platform pushes with its own key) are not
    deploy-key-over-SSH and never need the SSH reachability check."""
    u = (uri or "").strip()
    if u.startswith("ssh://"):
        return True
    if "://" in u:  # http(s):// or any other scheme
        return False
    # scp-like `user@host:path`: an `@`, and a `:` whose left side carries no `/`.
    host, sep, _ = u.partition(":")
    return sep == ":" and "@" in host and "/" not in host


def normalize_ssh_uri(uri: str | None) -> str:
    """Canonicalise a git SSH remote. git's scp-like syntax has NO port field, so
    `git@host:10022/grp/repo.git` - the form everyone writes after reading a
    self-hosted GitLab's "SSH port 10022" - is parsed as PATH `10022/grp/repo.git`
    on port 22: it dials the wrong port and asks for a repository that doesn't
    exist, and git's own error only says the repository may not exist. Rewrite
    that one shape to the URL form (`ssh://git@host:10022/grp/repo.git`), which
    means what the customer intended. Everything else is returned untouched -
    plain scp-like remotes are valid, and https/ssh:// URLs already say it."""
    u = (uri or "").strip()
    if not u or not is_ssh_uri(u) or u.startswith("ssh://"):
        return u
    host, _, path = u.partition(":")
    port, sep, rest = path.partition("/")
    if not (sep and port.isdigit() and rest):
        return u
    return f"ssh://{host}:{port}/{rest}"


def git_host_rewrite(remote: str) -> list[str]:
    """`git -c url.<mapped>.insteadOf=<original>` args routing a PLATFORM-side git
    transport (worker root-workspace refresh, merge probe, the API's SSH checks) to
    GIT_EXTRA_HOST's target - the same tailnet mapping the deployer injects into
    runner sandboxes as hostAliases. api/worker pods have no such alias: on
    Kubernetes a tailnet-only git host resolves publicly and its SSH port times
    out, which parked DELIVERED runs as failed and made the repo card's Verify SSH
    report an unreachable host for a correctly installed deploy key. Handles both
    remote spellings; no-op when the setting is empty or the host doesn't match."""
    alias, _, mapped = (settings.git_extra_host or "").partition(":")
    if not alias or not mapped:
        return []
    remote = (remote or "").strip()
    if remote.startswith("ssh://"):
        try:
            parts = urllib.parse.urlsplit(remote)
        except ValueError:
            return []
        if parts.hostname != alias:
            return []
        user = f"{parts.username}@" if parts.username else ""
        port = f":{parts.port}" if parts.port else ""
        return ["-c", (f"url.ssh://{user}{mapped}{port}/"
                       f".insteadOf=ssh://{user}{alias}{port}/")]
    if not is_ssh_uri(remote):
        return []
    # scp-like `user@alias:path` - the path is relative to the git user's home,
    # which the ssh:// form spells with a leading slash.
    head, _, _ = remote.partition(":")
    user, _, host = head.rpartition("@")
    if host != alias:
        return []
    user = f"{user}@" if user else ""
    return ["-c", f"url.ssh://{user}{mapped}/.insteadOf={user}{alias}:"]


def _ssh_cause(err: str) -> str:
    """The line of git's stderr that names the failure. ssh states the cause
    FIRST ("connect to host … Connection timed out"); git closes with its generic
    "make sure you have the correct access rights and the repository exists",
    which is what a tail picks up and reads like an auth problem for a host that
    never answered at all."""
    lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
    if not lines:
        return "unknown error"
    for line in lines:
        if line.lower().startswith(("ssh:", "fatal: could not read", "git@", "permission denied")):
            return line
    return lines[0]


def check_ssh(ssh_uri: str, deploy_private_key: str, write: bool = False) -> tuple[bool, str]:
    """Reachability + deploy-key-access check for a customer's own remote repo,
    run BEFORE development so a missing/mis-added deploy key surfaces here instead
    of failing the dev run at push time. Runs `git ls-remote --heads <ssh_uri>`
    over SSH with the project's deploy key in a throwaway tempdir, BatchMode (never
    prompts), accept-new host key, tight timeout. Returns (ok, detail); never leaks
    the key. Only meaningful for an SSH URI (git@… / ssh://) - an https:// or the
    platform repo returns a clear, non-actionable note. `write` (the push target)
    goes on to prove the key can PUSH (check_push): reading is what a deploy key
    installed without write access, or one whose installer lost their rights,
    still does fine - the refusal only came at the end of a full build. Shells
    out, so callers run it in a threadpool."""
    uri = normalize_ssh_uri(ssh_uri)
    if not uri:
        return False, "No repository URL to check."
    if not is_ssh_uri(uri):
        return False, ("SSH reachability applies to an SSH remote (git@host:path). This "
                       "repository is reached over HTTPS or managed by the platform, so it "
                       "doesn't use the project's deploy key.")
    key = (deploy_private_key or "").strip()
    if not key:
        return False, "This project has no deploy key yet - it's generated when the project is created."
    try:
        with tempfile.TemporaryDirectory() as td:
            keyfile = Path(td) / "id"
            keyfile.write_text(key + "\n")
            keyfile.chmod(0o600)
            env = {**os.environ,
                   "GIT_SSH_COMMAND": (f"ssh -i {keyfile} -o IdentitiesOnly=yes "
                                       "-o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                                       "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"),
                   "GIT_TERMINAL_PROMPT": "0"}
            proc = subprocess.run(["git", *git_host_rewrite(uri), "ls-remote", "--heads", uri],
                                  env=env, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False, "Couldn't reach the host in time (timed out). Check the URL and that the host is reachable."
    except Exception as exc:  # pragma: no cover - defensive; git missing etc.
        return False, f"Couldn't run the SSH check ({exc})."
    if proc.returncode == 0:
        if not write:
            return True, "Reachable. The deploy key has access to this repository."
        verdict, detail = check_push(uri, key, "verify")
        if verdict == "ok":
            return True, "Reachable, and the deploy key can push to this repository."
        if verdict == "denied":
            return False, (f"Reachable, but the deploy key can't push: {detail} The working "
                           "repository needs the key installed with write access, by an "
                           "account that keeps push rights there.")
        return True, f"Reachable; the push probe couldn't run ({detail})."
    err = (proc.stderr or "").strip()
    detail = _ssh_cause(err)
    low = err.lower()
    if "permission denied" in low or "publickey" in low:
        return False, ("The repository isn't authorized for this project's deploy key yet - add "
                       "the public key shown above as a deploy key on the repository, then retry.")
    if ("could not resolve" in low or "name or service not known" in low
            or "no route to host" in low or "connection refused" in low
            or "connection timed out" in low or "network is unreachable" in low
            or "timed out" in low):
        return False, f"Couldn't reach the host ({detail})."
    if "repository not found" in low or "does not appear to be a git repository" in low:
        return False, ("Reached the host, but the repository path wasn't found - check the URL "
                       "(and that the deploy key is on the intended repository).")
    return False, f"SSH check failed ({detail})."


PREFLIGHT_REF = "refs/openvisor/preflight"
_PROBE_TIMEOUT_S = 45


def _push_cause(err: str) -> str:
    """The line of a refused push that names the refusal. The forge speaks
    through `remote:` lines (GitLab: "You are not allowed to push code to this
    project."), GitHub's ssh front-end through a bare `ERROR:` line ("The key you
    are authenticating with has been marked as read only."), ssh itself through
    its first line; git's own closing advice is never the cause."""
    lines = [ln.strip() for ln in (err or "").splitlines()
             if ln.strip() and not ln.startswith("Warning: Permanently added")]
    for ln in lines:
        if ln.lower().startswith("remote:"):
            body = ln[len("remote:"):].strip()
            if body and not set(body) <= set("=-*"):
                return body
    for ln in lines:
        if ln.startswith("ERROR:"):
            return ln
    for ln in lines:
        if not ln.startswith(("!", "To ", "error: failed to push", "fatal: Could not read")):
            return ln
    return lines[0] if lines else "the remote answered nothing"


def check_push(ssh_uri: str, deploy_private_key: str, probe: str,
               author: tuple[str, str] | None = None) -> tuple[str, str]:
    """Prove the deploy key can PUSH to a remote, before anything is built on
    it: push an empty commit to `refs/openvisor/preflight/<probe>` (a hidden
    ref - no branch, no CI, nothing in any UI) and delete it again. The remote
    runs its real pre-receive checks on it, so this answers exactly what the
    final branch push would hear: a read-only key, a key whose installer no
    longer has push rights (GitLab authorises deploy-key pushes as the account
    that installed the key - a project transferred out of that account's group
    keeps the key but refuses its pushes), a repository that moved. Returns
    (verdict, detail): `ok`, `denied` (the customer's to fix; detail quotes the
    remote), `unreachable` (network - not a verdict on the key), `error`.
    Shells out; callers run it off the event loop."""
    uri = normalize_ssh_uri(ssh_uri)
    key = (deploy_private_key or "").strip()
    if not uri or not is_ssh_uri(uri):
        return "error", "not an SSH remote"
    if not key:
        return "error", "no deploy key"
    ref = f"{PREFLIGHT_REF}/{re.sub(r'[^A-Za-z0-9._-]', '-', probe)[:64] or 'probe'}"
    name, email = author or (f"{settings.brand_name} agent", f"agent@{settings.deploy_domain}")
    try:
        with tempfile.TemporaryDirectory() as td:
            keyfile = Path(td) / "id"
            keyfile.write_text(key + "\n")
            keyfile.chmod(0o600)
            env = {**os.environ,
                   "GIT_SSH_COMMAND": (f"ssh -i {keyfile} -o IdentitiesOnly=yes "
                                       "-o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                                       "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"),
                   "GIT_TERMINAL_PROMPT": "0",
                   "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                   "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
            proc = _probe_push(uri, Path(td) / "probe", ref, env)
    except subprocess.TimeoutExpired:
        return "unreachable", "Couldn't reach the host in time (timed out)."
    except Exception as exc:  # pragma: no cover - defensive; git missing etc.
        return "error", f"Couldn't run the push probe ({exc})."
    if proc.returncode == 0:
        return "ok", "The deploy key can push to this repository."
    err = (proc.stderr or "").strip()
    detail = _push_cause(err)
    low = err.lower()
    if ("could not resolve" in low or "name or service not known" in low
            or "no route to host" in low or "connection refused" in low
            or "connection timed out" in low or "network is unreachable" in low
            or "timed out" in low):
        return "unreachable", f"Couldn't reach the host ({detail})."
    return "denied", detail


def _probe_push(uri: str, repo: Path, ref: str, env: dict) -> subprocess.CompletedProcess:
    """The git side of check_push, on any remote git accepts: init, one empty
    commit, push it to `ref`, delete `ref` (best-effort - a refused deletion
    leaves a hidden ref the next probe overwrites). The push's result is the
    verdict."""
    rw = git_host_rewrite(uri)
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True,
                   capture_output=True, timeout=15)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
                    "-m", "preflight probe"], env=env, check=True,
                   capture_output=True, timeout=15)
    proc = subprocess.run(["git", *rw, "-C", str(repo), "push", "-q", uri, f"HEAD:{ref}"],
                          env=env, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    if proc.returncode == 0:
        subprocess.run(["git", *rw, "-C", str(repo), "push", "-q", uri, f":{ref}"],
                       env=env, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    return proc


def replace_repo_path(uri: str, new_path: str) -> str:
    """The same remote, pointed at a repository that moved: keep the scheme,
    user, host and port the customer connected (their SSH alias, their tailnet
    name) and swap only the project path. `ssh://git@host:10022/old/name.git` →
    `ssh://git@host:10022/new/name.git`; scp-like and https spellings alike."""
    u = (uri or "").strip()
    path = new_path.strip().strip("/")
    if not u or not path:
        return u
    suffix = ".git" if u.rstrip("/").endswith(".git") else ""
    if "://" in u:
        parts = urllib.parse.urlsplit(u)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, f"/{path}{suffix}",
                                        "", ""))
    if is_ssh_uri(u):
        head, _, _ = u.partition(":")
        return f"{head}:{path}{suffix}"
    return u


def detect_default_branch(ssh_uri: str, deploy_private_key: str) -> str | None:
    """The remote's real default branch (its HEAD symref) over SSH with the
    project's deploy key - `git ls-remote --symref <uri> HEAD` prints
    `ref: refs/heads/<name>\tHEAD`. Hardcoding "main" broke repos whose default
    is "master": the runner read the mismatch as an empty remote and built an
    orphan branch. None on any failure (caller falls back); provider-agnostic
    (works on github/gitlab/other alike, no API token needed)."""
    uri = normalize_ssh_uri(ssh_uri)
    key = (deploy_private_key or "").strip()
    if not uri or not key or not is_ssh_uri(uri):
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            keyfile = Path(td) / "id"
            keyfile.write_text(key + "\n")
            keyfile.chmod(0o600)
            env = {**os.environ,
                   "GIT_SSH_COMMAND": (f"ssh -i {keyfile} -o IdentitiesOnly=yes "
                                       "-o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                                       "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"),
                   "GIT_TERMINAL_PROMPT": "0"}
            proc = subprocess.run(["git", *git_host_rewrite(uri), "ls-remote", "--symref",
                                   uri, "HEAD"], env=env,
                                  capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001 - timeout, git missing: caller falls back
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ref:") and line.rstrip().endswith("HEAD"):
            ref = line.split()[1]
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]
    return None


# ---- HTTP(S) git remotes (knowledge-base git sources, §KB) ----

_USERINFO_RE = re.compile(r"(https?://)[^/@\s]+@")


def redact_secret(text: str, *secrets: str) -> str:
    """Strip any embedded credentials from a git error string before it is stored
    or returned: every provided secret value, plus any `scheme://user:pass@` userinfo
    (belt-and-braces so a PAT injected into a clone URL never surfaces in a message)."""
    out = text or ""
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return _USERINFO_RE.sub(r"\1***@", out)


def https_with_pat(uri: str, pat: str, username: str | None = None) -> str:
    """Inject a token into an https:// git URL as userinfo (`https://<user>:<pat>@host/path`).
    The token rides as the PASSWORD; `username` defaults to `oauth2`, which works for
    GitHub and GitLab PATs (GitLab rejects a token-as-username/empty-password
    credential with a 401, GitHub accepts any username when the password is a token).
    Credentials that CHECK the username - a GitLab deploy token's generated
    `gitlab+deploy-token-N`, a Bitbucket app password's real account name - pass
    theirs explicitly. Both halves are percent-encoded so odd characters can't
    corrupt the URL; never logged or stored."""
    u = (uri or "").strip()
    parts = urllib.parse.urlsplit(u)
    host = parts.netloc.rsplit("@", 1)[-1]  # drop any userinfo already present
    token = urllib.parse.quote(pat or "", safe="")
    user = urllib.parse.quote((username or "").strip() or "oauth2", safe="")
    netloc = f"{user}:{token}@{host}" if token else host
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def check_http_git(uri: str, pat: str, username: str | None = None) -> tuple[bool, str]:
    """Reachability + auth check for an HTTP(S) git remote using a PAT, run before a
    git knowledge source is enabled so a bad token/URL surfaces here instead of at
    reindex time. `git ls-remote --heads` with the PAT injected into the URL, prompts
    disabled, tight timeout. Returns (ok, detail) - the PAT is NEVER leaked into the
    detail string. Shells out, so callers run it in a threadpool."""
    u = (uri or "").strip()
    if not u:
        return False, "No repository URL to check."
    if not re.match(r"^https?://", u, re.I):
        return False, "An HTTP(S) git source needs a URL starting with http:// or https://."
    token = (pat or "").strip()
    if not token:
        return False, "No access token stored for this source."
    try:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
               "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
        proc = subprocess.run(["git", "ls-remote", "--heads", https_with_pat(u, token, username)],
                              env=env, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False, "Couldn't reach the host in time (timed out). Check the URL and that the host is reachable."
    except Exception as exc:  # pragma: no cover - defensive; git missing etc.
        return False, f"Couldn't run the connection check ({redact_secret(str(exc), token)})."
    if proc.returncode == 0:
        return True, "Reachable. The access token can read this repository."
    err = redact_secret((proc.stderr or "").strip(), token)
    detail = err.splitlines()[-1].strip() if err else "unknown error"
    low = err.lower()
    if ("authentication failed" in low or "invalid username or password" in low
            or "403" in low or "401" in low or "could not read" in low
            or "terminal prompts disabled" in low):
        return False, "Authentication failed - check the access token has read access to this repository."
    if ("could not resolve" in low or "name or service not known" in low
            or "no route to host" in low or "connection refused" in low
            or "connection timed out" in low or "network is unreachable" in low
            or "timed out" in low):
        return False, f"Couldn't reach the host ({detail})."
    if "not found" in low or "404" in low or "repository not found" in low:
        return False, "Repository not found - check the URL (and that the token can see it)."
    return False, f"Connection check failed ({detail})."
