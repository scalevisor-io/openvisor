"""GitHub provider for customer-owned repos (§14, GitHub variant of gitlab.py).

Development for a project whose primary repo is a github.com URL pushes the
agent branch over the project's deploy key (SSH) and uses the REST API for
everything a deploy key can't do: seeding the base branch of an empty repo and
opening / inspecting / merging the pull request. Every call takes an optional
`token`; the worker resolves it per project (a GITHUB_TOKEN Memory secret) and
falls back to the platform-wide settings.github_token. Sync httpx, called from
Celery. Failures raise GitHubError; callers degrade gracefully so the platform
keeps working when GitHub (or the token) is unavailable."""
import base64
import logging
import re

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubError(Exception):
    pass


def is_github(uri: str | None) -> bool:
    return bool(uri) and "github.com" in uri


def parse_repo(uri: str) -> tuple[str, str]:
    """(owner, repo) from git@github.com:owner/repo.git or https://github.com/owner/repo(.git)."""
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", uri.strip())
    if not m:
        raise GitHubError(f"unrecognised GitHub URL: {uri}")
    return m.group(1), m.group(2)


def _client(token: str | None = None) -> httpx.Client:
    tok = token or settings.github_token
    if not tok:
        raise GitHubError("GITHUB_TOKEN not configured")
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=30,
    )


def check_repo_access(owner: str, repo: str, token: str) -> tuple[bool, str]:
    """Auth check: GET /repos/{owner}/{repo} with the token confirms it is valid
    AND can access THAT repo (gates saving auto_merge). Returns (ok, human detail).
    A repo the token can't see 404s (GitHub hides private repos), which we report
    as 'not found or no access'."""
    if not token:
        return False, "Add a GITHUB_TOKEN secret in Memory first."
    try:
        with _client(token) as c:
            r = c.get(f"/repos/{owner}/{repo}")
    except GitHubError as exc:
        return False, str(exc)
    except httpx.HTTPError as exc:
        return False, f"Could not reach GitHub: {exc}"
    if r.status_code == 200:
        return True, f"Authenticated - access to {owner}/{repo} confirmed."
    if r.status_code == 401:
        return False, "The GITHUB_TOKEN is invalid or expired."
    if r.status_code in (403, 404):
        return False, "Repository not found, or the token lacks access to it (needs repo scope)."
    return False, f"GitHub returned {r.status_code}."


def repo_is_empty(owner: str, repo: str, token: str | None = None) -> bool:
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/branches")
        r.raise_for_status()
        return len(r.json()) == 0


def ensure_base_branch(owner: str, repo: str, branch: str = "main",
                       token: str | None = None) -> None:
    """Guarantee `branch` exists so a PR has a base. On a brand-new empty repo,
    seed it with a README commit (the Contents API creates the default branch in
    one call). Idempotent: no-op once the branch exists."""
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/branches/{branch}")
        if r.status_code == 200:
            return
        content = base64.b64encode(
            f"# {repo}\n\nManaged by {settings.brand_name}. The MVP lands here via pull request.\n"
            .encode()).decode()
        r = c.put(f"/repos/{owner}/{repo}/contents/README.md", json={
            "message": f"{settings.brand_name}: initialise repository",
            "content": content, "branch": branch})
        if r.status_code not in (200, 201):
            raise GitHubError(f"seed base branch failed: {r.status_code} {r.text[:300]}")
        log.info("seeded %s/%s base branch %s", owner, repo, branch)


def list_prs_for_branch(owner: str, repo: str, head_branch: str,
                        token: str | None = None) -> list[dict]:
    """Every PR whose head is head_branch (same repo), in ANY state, newest
    first (§delivery reconciler: the change is found by its branch, not by a
    pointer the platform may since have cleared). The list payload carries
    `merged_at` rather than `merged`; readers normalise."""
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/pulls",
                  params={"state": "all", "head": f"{owner}:{head_branch}",
                          "sort": "created", "direction": "desc", "per_page": 20})
        r.raise_for_status()
        return r.json()


def close_pr(owner: str, repo: str, number: int, token: str | None = None) -> None:
    """Close an open PR without merging (§delivery reconciler: Start fresh
    discards the request's open change)."""
    with _client(token) as c:
        r = c.patch(f"/repos/{owner}/{repo}/pulls/{number}", json={"state": "closed"})
        r.raise_for_status()


def find_open_pr(owner: str, repo: str, head_branch: str,
                 token: str | None = None) -> dict | None:
    """Open PR whose head is head_branch (in the same repo, so head=owner:branch)."""
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/pulls",
                  params={"state": "open", "head": f"{owner}:{head_branch}"})
        r.raise_for_status()
        prs = r.json()
        return prs[0] if prs else None


def open_pr(owner: str, repo: str, head_branch: str, base_branch: str,
            title: str, body: str, token: str | None = None) -> dict:
    """Open a PR head_branch → base_branch, or return the existing open one.
    Returns {number, html_url, ...}."""
    existing = find_open_pr(owner, repo, head_branch, token=token)
    if existing:
        return existing
    with _client(token) as c:
        # head is ALWAYS owner-qualified: an unqualified head on a forked repo
        # resolves against the parent and 422s ("field: head, code: invalid")
        # even though the branch exists - seen live on a customer fork. The
        # qualified form is valid for plain same-repo PRs too (and matches the
        # find_open_pr filter above).
        r = c.post(f"/repos/{owner}/{repo}/pulls", json={
            "title": title, "body": body, "head": f"{owner}:{head_branch}",
            "base": base_branch})
        if r.status_code == 201:
            return r.json()
        # A 422 usually means "no commits between base and head" (agent produced
        # nothing) or the PR already exists - re-check before failing.
        again = find_open_pr(owner, repo, head_branch, token=token)
        if again:
            return again
        raise GitHubError(f"open PR failed: {r.status_code} {r.text[:300]}")


def branch_exists(owner: str, repo: str, branch: str, token: str | None = None) -> bool:
    """Whether the branch still exists on the repo - a published branch the
    customer deleted (closed PR) reads as rejected work (§branch naming)."""
    import urllib.parse
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/branches/{urllib.parse.quote(branch, safe='')}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


def branch_ahead_of_base(owner: str, repo: str, branch: str, base: str,
                         token: str | None = None) -> bool:
    """Whether `branch` carries commits `base` does not (§14 resume-publish):
    the signal that a pushed branch still holds UNPUBLISHED work. A 404 means
    the compare has no base to stand on (an uninitialized repo whose default
    branch was never born) - with the branch itself already checked to exist,
    that too means everything on it is unpublished, so it answers True."""
    import urllib.parse
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/compare/"
                  f"{urllib.parse.quote(base, safe='')}..."
                  f"{urllib.parse.quote(branch, safe='')}")
        if r.status_code == 404:
            return True
        r.raise_for_status()
        return int(r.json().get("ahead_by") or 0) > 0


def list_open_issues(owner: str, repo: str, token: str | None = None) -> list[dict]:
    """Open issues normalized for the §auto_dev sweep: {iid, url, title, body,
    labels, assignees, author}. GitHub's issues API returns PRs too - filtered out."""
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/issues",
                  params={"state": "open", "per_page": 100})
        r.raise_for_status()
        out = []
        for it in r.json():
            if "pull_request" in it:
                continue
            out.append({
                "iid": it["number"],
                "url": it.get("html_url", ""),
                "title": it.get("title", ""),
                "body": it.get("body") or "",
                "labels": [l.get("name", "") for l in it.get("labels", []) or []
                           if isinstance(l, dict)],
                "assignees": [a.get("login", "") for a in it.get("assignees", []) or []],
                "author": (it.get("user") or {}).get("login", ""),
            })
        return out


def resolve_moved(owner: str, repo: str, token: str | None = None) -> tuple[str, str] | None:
    """The repository's CURRENT (owner, name) when `owner/repo` is the redirect a
    rename or a transfer left behind, else None. GitHub answers the old name with
    301 on the API (git keeps following it with a warning, the API does not), so
    the connected row learns the new name here. Never raises."""
    try:
        with _client(token) as c:
            r = c.get(f"/repos/{owner}/{repo}")
            if r.status_code not in (301, 302, 307, 308):
                return None
            r = c.get(f"/repos/{owner}/{repo}", follow_redirects=True)
            if r.status_code != 200:
                return None
            full = str((r.json() or {}).get("full_name") or "").strip("/")
    except (httpx.HTTPError, ValueError, GitHubError):
        return None
    if "/" not in full or full.lower() == f"{owner}/{repo}".lower():
        return None
    new_owner, _, new_repo = full.partition("/")
    return new_owner, new_repo


def _key_body(public_key: str | None) -> str:
    parts = (public_key or "").split()
    return parts[1] if len(parts) >= 2 else (parts[0] if parts else "")


def ensure_deploy_key(owner: str, repo: str, title: str, public_key: str,
                      token: str | None = None) -> str:
    """Make the project's deploy key usable on a GitHub repo - installed with
    write access - through the customer's token (repo admin). POST first; a copy
    already on THIS repo (read-only, say) is what makes GitHub answer 422 "key
    is already in use", and only then is it removed and the key re-added (a
    GitHub deploy key belongs to one repository, so the removal frees the
    fingerprint and the re-add cannot be refused for it). A key in use on
    ANOTHER repository is reported, never touched. Returns "installed" |
    "reinstalled" | "already installed"; raises GitHubError with the API's own
    words - and then nothing was changed."""
    body = _key_body(public_key)
    if not body:
        raise GitHubError("the project has no deploy key")
    with _client(token) as c:
        payload = {"title": title, "key": public_key.strip(), "read_only": False}
        r = c.post(f"/repos/{owner}/{repo}/keys", json=payload)
        if r.status_code == 201:
            return "installed"
        if r.status_code != 422:
            raise GitHubError(f"deploy key install failed: HTTP {r.status_code} {r.text[:160]}")
        keys = c.get(f"/repos/{owner}/{repo}/keys", params={"per_page": 100})
        if keys.status_code != 200:
            raise GitHubError(f"deploy keys unreadable: HTTP {keys.status_code} {keys.text[:160]}")
        here = next((k for k in keys.json() if _key_body(k.get("key")) == body), None)
        if here is None:
            raise GitHubError("the deploy key is already in use on another GitHub repository "
                              "(a deploy key belongs to one repository)")
        if not here.get("read_only"):
            return "already installed"
        d = c.delete(f"/repos/{owner}/{repo}/keys/{here['id']}")
        if d.status_code not in (204, 404):
            raise GitHubError(f"deploy key removal failed: HTTP {d.status_code} {d.text[:160]}")
        r = c.post(f"/repos/{owner}/{repo}/keys", json=payload)
        if r.status_code != 201:
            raise GitHubError(f"deploy key re-add failed: HTTP {r.status_code} {r.text[:160]}")
        return "reinstalled"


def create_issue_comment(owner: str, repo: str, number: int, body: str,
                         token: str | None = None) -> None:
    with _client(token) as c:
        r = c.post(f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})
        r.raise_for_status()


def get_pr(owner: str, repo: str, number: int, token: str | None = None) -> dict:
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/pulls/{number}")
        r.raise_for_status()
        return r.json()


def pr_diff(owner: str, repo: str, number: int, token: str | None = None) -> str:
    """The PR's unified diff (the raw `.diff` media type), fed to the §14.7
    security review. Bounded by GitHub to 300 files / 20k lines; larger PRs
    return 406, which we surface as a GitHubError so the caller fails closed."""
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/pulls/{number}",
                  headers={"Accept": "application/vnd.github.diff"})
        if r.status_code != 200:
            raise GitHubError(f"pr diff failed: {r.status_code} {r.text[:200]}")
        return r.text


def pr_change_summary(owner: str, repo: str, number: int, token: str | None = None,
                      max_files: int = 40, max_commits: int = 20) -> dict:
    """§work answers: what a PR actually changed, as facts the chat can explain -
    commit subjects + per-file line counts, never diff content (an answer quoting
    the diff would ship code into the immutable chat log). The caller treats any
    failure as "no git facts available"."""
    with _client(token) as c:
        rc = c.get(f"/repos/{owner}/{repo}/pulls/{number}/commits",
                   params={"per_page": max_commits})
        rc.raise_for_status()
        rf = c.get(f"/repos/{owner}/{repo}/pulls/{number}/files",
                   params={"per_page": max_files})
        rf.raise_for_status()
    return {
        "commits": [(cm.get("commit", {}).get("message") or "").splitlines()[0][:200]
                    for cm in rc.json()[:max_commits]],
        "files": [{"path": f.get("filename", ""), "status": f.get("status", ""),
                   "added": f.get("additions", 0), "removed": f.get("deletions", 0)}
                  for f in rf.json()[:max_files]],
    }


def commits_contained_in(owner: str, repo: str, base: str, head_sha: str,
                         token: str | None = None) -> bool:
    """True when head_sha is already reachable from base: the PR's commits
    landed in the base branch without GitHub marking the PR merged - e.g. the
    customer resolved a conflict locally, merged, and pushed base directly.
    compare status 'identical'/'behind' = contained."""
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/compare/{base}...{head_sha}")
        r.raise_for_status()
        return r.json().get("status") in ("identical", "behind")


def update_pr_body(owner: str, repo: str, number: int, body: str,
                   token: str | None = None) -> None:
    """Replace an open PR's body (§PR description parity: a revise run's fresh
    pr.md must reach the displayed description - open_pr returns a pre-existing
    PR untouched)."""
    with _client(token) as c:
        r = c.patch(f"/repos/{owner}/{repo}/pulls/{number}", json={"body": body})
        r.raise_for_status()


def merge_pr(owner: str, repo: str, number: int, method: str = "squash",
             token: str | None = None) -> tuple[bool, str]:
    """Merge a PR (admin/auto path). The customer normally merges via their own
    tooling; this is here for symmetry with gitlab.auto_merge. 405 = the repo's
    settings forbid `method`; fall back to a plain merge commit (better merged
    un-squashed than parked)."""
    with _client(token) as c:
        r = c.put(f"/repos/{owner}/{repo}/pulls/{number}/merge",
                  json={"merge_method": method})
        if r.status_code == 200 and r.json().get("merged"):
            return True, "merged"
        if method != "merge" and r.status_code == 405:
            r = c.put(f"/repos/{owner}/{repo}/pulls/{number}/merge",
                      json={"merge_method": "merge"})
            if r.status_code == 200 and r.json().get("merged"):
                return True, f"merged (repository does not allow {method})"
        return False, f"merge blocked: {r.status_code} {r.text[:200]}"


def ci_status(owner: str, repo: str, sha: str, token: str | None = None) -> str:
    """The head commit's CI verdict for the merge sweep's CI watch (§14.10):
    'failure' | 'success' | 'pending' | 'none'. GitHub reports CI through two
    disjoint APIs - the legacy combined commit status and check runs (GitHub
    Actions reports through the latter) - so both are read and any failure wins.
    A cancelled or action_required check is neither: it is not the agent's to
    fix, so it never triggers a fix run. A fine-grained PAT often grants only
    ONE of the two surfaces ("Commit statuses" vs "Checks" read), so each is
    optional independently - a 403/404 contributes nothing rather than blinding
    the whole verdict; only both-unreadable raises (the sweep skips that tick)."""
    combined: dict = {}
    runs: list = []
    denied: httpx.HTTPStatusError | None = None
    with _client(token) as c:
        rs = c.get(f"/repos/{owner}/{repo}/commits/{sha}/status")
        try:
            rs.raise_for_status()
            combined = rs.json()
        except httpx.HTTPStatusError as exc:
            if rs.status_code not in (403, 404):
                raise
            denied = exc
        rc = c.get(f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
                   params={"per_page": 100})
        try:
            rc.raise_for_status()
            runs = rc.json().get("check_runs", [])
        except httpx.HTTPStatusError as exc:
            if rc.status_code not in (403, 404):
                raise
            if denied is not None:
                raise denied
    states: list[str] = []
    if combined.get("total_count"):
        states.append({"success": "success", "pending": "pending"}.get(
            combined.get("state"), "failure"))
    for run in runs:
        if run.get("status") != "completed":
            states.append("pending")
        elif run.get("conclusion") in ("failure", "timed_out"):
            states.append("failure")
        elif run.get("conclusion") in ("success", "neutral", "skipped"):
            states.append("success")
    if "failure" in states:
        return "failure"
    if "pending" in states:
        return "pending"
    return "success" if states else "none"


def failed_ci_logs(owner: str, repo: str, sha: str, token: str | None = None,
                   max_chars: int = 6000) -> str:
    """gitlab.failed_pipeline_logs' GitHub sibling: what failed on the head
    commit, for the sweep's CI-fix instruction. Actions job logs first (the real
    traces); check-run output and summaries as the fallback for third-party CI.
    Best-effort - any hole in the trail just shortens the text, and the caller
    has a generic instruction for an empty one."""
    out: list[str] = []
    with _client(token) as c:
        try:
            runs = c.get(f"/repos/{owner}/{repo}/actions/runs",
                         params={"head_sha": sha, "per_page": 20}
                         ).json().get("workflow_runs", [])
            for wr in runs:
                if wr.get("conclusion") not in ("failure", "timed_out"):
                    continue
                jobs = c.get(f"/repos/{owner}/{repo}/actions/runs/{wr['id']}/jobs",
                             params={"per_page": 50}).json().get("jobs", [])
                for job in jobs:
                    if job.get("conclusion") not in ("failure", "timed_out"):
                        continue
                    r = c.get(f"/repos/{owner}/{repo}/actions/jobs/{job['id']}/logs",
                              follow_redirects=True)
                    if r.status_code == 200 and r.text:
                        out.append(f"### job '{job.get('name')}' failed\n"
                                   f"{r.text[-max_chars:]}")
        except (httpx.HTTPError, ValueError):
            pass
        if not out:
            try:
                checks = c.get(f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
                               params={"per_page": 100}).json().get("check_runs", [])
                for run in checks:
                    if run.get("conclusion") not in ("failure", "timed_out"):
                        continue
                    o = run.get("output") or {}
                    text = "\n".join(filter(None, [o.get("title"), o.get("summary"),
                                                   o.get("text")]))
                    out.append(f"### check '{run.get('name')}' failed\n"
                               f"{text[:max_chars]}".rstrip())
            except (httpx.HTTPError, ValueError):
                pass
    return "\n\n".join(out)[-max_chars:]
