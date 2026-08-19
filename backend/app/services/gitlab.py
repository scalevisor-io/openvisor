"""Self-hosted GitLab provisioning (PROMPT §9.10-11). Sync httpx, called from
Celery. Failures are logged and surfaced as pending - the platform must keep
working when GitLab is unreachable (local dev)."""
import io
import logging
import re
import secrets
import tarfile
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)


class GitLabError(Exception):
    pass


def _host(uri: str | None) -> str | None:
    """Host from a git@host:group/name.git or https://host/group/name(.git) URL."""
    if not uri:
        return None
    s = uri.strip()
    m = re.match(r"^[A-Za-z0-9._-]+@([^:/]+):", s)  # scp-like ssh (git@host:path)
    if m:
        return m.group(1).lower()
    parts = urlsplit(s if "://" in s else f"ssh://{s}")
    return (parts.hostname or "").lower() or None


def is_platform_host(uri: str | None) -> bool:
    """True when the URL points at the platform's OWN GitLab (§ssh remotes).

    The instance knows its forge by two names and they need not match: the API
    host from GITLAB_URL (`gitlab.example.com`, what /api/v4 answers on) and the
    SSH host from GITLAB_SSH_HOST (`git.example.com:10022`, what git dials). A
    repo cloned over the SSH name is still OUR project - recognising that is what
    lets its API calls use the reachable API host and the platform token instead
    of being treated as a stranger's forge."""
    host = _host(uri)
    if not host:
        return False
    api = _host(settings.gitlab_url) if settings.gitlab_url else None
    ssh = (settings.gitlab_ssh_host or "").strip().lower() or None
    return host == api or host == ssh


def is_gitlab(uri: str | None) -> bool:
    """True for a URL on a recognisable GitLab host: the configured platform
    hosts (API or SSH), gitlab.com, or a host named gitlab.* / *.gitlab.*.
    Conservative on purpose - an unrecognised self-hosted host stays 'other' and
    the customer picks the platform explicitly in the UI (detect_provider
    ambiguity path)."""
    host = _host(uri)
    if not host:
        return False
    return (is_platform_host(uri) or host == "gitlab.com"
            or host.startswith("gitlab.") or ".gitlab." in host)


def customer_base_url(uri: str) -> str:
    """The API base for a GitLab repo URL (host only, not path).

    A repo on the platform's own GitLab resolves to the configured GITLAB_URL:
    deriving `https://<ssh-host>` from the remote is wrong whenever the SSH and
    API hostnames differ - only the API one serves /api/v4, and on a tailnet
    deployment the SSH name is not even routable from the api/worker pods."""
    if is_platform_host(uri):
        return settings.gitlab_url.rstrip("/")
    host = _host(uri)
    if not host:
        raise GitLabError(f"unrecognised GitLab URL: {uri}")
    return f"https://{host}"


def parse_repo_path(uri: str) -> str:
    """'group/subgroup/name' project path from a GitLab git URL (ssh or https)."""
    s = uri.strip()
    m = re.match(r"^[A-Za-z0-9._-]+@[^:/]+:(.+?)(?:\.git)?/?$", s)  # scp ssh form
    if m:
        return m.group(1)
    parts = urlsplit(s if "://" in s else f"ssh://{s}")
    path = (parts.path or "").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        raise GitLabError(f"unrecognised GitLab URL: {uri}")
    return path


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=f"{settings.gitlab_url.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": settings.gitlab_token},
        timeout=30,
    )


def ensure_user(email: str) -> int:
    """Idempotent GitLab user creation keyed on email. Returns user id."""
    with _client() as c:
        r = c.get("/users", params={"search": email})
        r.raise_for_status()
        for u in r.json():
            if u.get("email") == email or u.get("username") == email.split("@")[0]:
                return u["id"]
        username = email.split("@")[0].replace(".", "-") + "-" + secrets.token_hex(3)
        r = c.post("/users", data={
            "email": email, "username": username, "name": email.split("@")[0],
            "password": secrets.token_urlsafe(16), "skip_confirmation": True,
        })
        r.raise_for_status()
        return r.json()["id"]


def _group_id(c: httpx.Client) -> int:
    r = c.get(f"/groups/{settings.gitlab_group.replace('/', '%2F')}")
    r.raise_for_status()
    return r.json()["id"]


def create_project(uuid_prefix: str, name: str) -> dict:
    """uuid-prefixed project in the configured group. Returns GitLab project JSON."""
    with _client() as c:
        gid = _group_id(c)
        r = c.post("/projects", data={
            "name": f"{uuid_prefix}-{name}"[:60],
            "path": f"{uuid_prefix}-{name}".lower().replace(" ", "-")[:60],
            "namespace_id": gid,
            "initialize_with_readme": True,
            "visibility": "private",
        })
        r.raise_for_status()
        return r.json()


def grant_read_access(project_id: int, user_id: int) -> None:
    with _client() as c:
        r = c.post(f"/projects/{project_id}/members",
                   data={"user_id": user_id, "access_level": 20})  # Reporter
        if r.status_code not in (201, 409):
            r.raise_for_status()


def add_deploy_key(project_id: int, title: str, public_key: str, can_push: bool = True) -> None:
    with _client() as c:
        r = c.post(f"/projects/{project_id}/deploy_keys",
                   data={"title": title, "key": public_key, "can_push": can_push})
        if r.status_code not in (201, 409):
            r.raise_for_status()


def get_project_by_path(path: str) -> dict:
    """Resolve a "group/name" path to the GitLab project JSON (id, web_url,
    default_branch, ...). Raises GitLabError when it doesn't exist or GitLab is
    unreachable - program creation must fail loud on a bad repo path."""
    try:
        with _client() as c:
            r = c.get(f"/projects/{quote(path, safe='')}")
            if r.status_code == 404:
                raise GitLabError(f"GitLab project '{path}' not found")
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError as exc:
        raise GitLabError(f"GitLab unreachable: {exc}") from exc


def read_raw_file(project_id: int, path: str, ref: str = "main") -> str | None:
    """Raw file content from a repo (README.md, input.template.yml). None when
    the file doesn't exist on that ref; GitLabError on transport failures."""
    try:
        with _client() as c:
            r = c.get(f"/projects/{project_id}/repository/files/{quote(path, safe='')}/raw",
                      params={"ref": ref})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
    except httpx.HTTPError as exc:
        raise GitLabError(f"GitLab unreachable: {exc}") from exc


def download_archive(project_id: int, dest_dir: str, ref: str = "main") -> None:
    """Extract the repo's tar.gz archive into dest_dir with the top-level
    "<name>-<sha>/" prefix stripped (so dest_dir IS the repo root). Used to
    materialize a program repo in a run workspace without git or credentials
    ever touching a command line. Member paths are traversal-guarded."""
    dest = Path(dest_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with _client() as c:
            r = c.get(f"/projects/{project_id}/repository/archive.tar.gz",
                      params={"sha": ref})
            r.raise_for_status()
            payload = r.content
    except httpx.HTTPError as exc:
        raise GitLabError(f"GitLab archive download failed: {exc}") from exc
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) <= 1:
                continue  # the top-level "<name>-<sha>" dir itself
            rel = Path(*parts[1:])
            target = (dest / rel).resolve()
            if not target.is_relative_to(dest):
                raise GitLabError(f"archive member escapes destination: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is not None:
                    target.write_bytes(src.read())
            # symlinks/devices are skipped on purpose (no legitimate use in a
            # program repo; a link could point the sandbox at platform files)


def platform_branch_exists(project_id: int, branch: str) -> bool:
    """Whether the branch exists on the platform repo (§14 agent-self-push)."""
    with _client() as c:
        r = c.get(f"/projects/{project_id}/repository/branches/{quote(branch, safe='')}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


def open_mr(project_id: int, source_branch: str, target_branch: str, title: str,
            description: str = "") -> dict:
    """Open a platform MR worker-side (§14 agent-self-push): when the agent
    pushed its branch itself, the entrypoint's push is a no-op and the
    MR-creating push options never fire - this is the fallback. A 409 (already
    exists, race with a push option) resolves to the existing open MR."""
    with _client() as c:
        r = c.post(f"/projects/{project_id}/merge_requests", json={
            "source_branch": source_branch, "target_branch": target_branch,
            "title": title, "description": description, "squash": True})
        if r.status_code == 409:
            existing = find_open_mr(project_id, source_branch)
            if existing:
                return existing
        r.raise_for_status()
        return r.json()


def find_open_mr(project_id: int, source_branch: str) -> dict | None:
    with _client() as c:
        r = c.get(f"/projects/{project_id}/merge_requests",
                  params={"state": "opened", "source_branch": source_branch})
        r.raise_for_status()
        mrs = r.json()
        return mrs[0] if mrs else None


def upload_file(project_id: int, filename: str, data: bytes) -> str:
    """Platform-GitLab twin of customer_upload_file: markdown for an uploaded
    file (§After-shots)."""
    with _client() as c:
        r = c.post(f"/projects/{project_id}/uploads",
                   files={"file": (filename, data, "image/png")})
        r.raise_for_status()
        return r.json()["markdown"]


def create_mr_note(project_id: int, mr_iid: int, body: str) -> None:
    with _client() as c:
        r = c.post(f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
                   json={"body": body})
        r.raise_for_status()


def update_mr(project_id: int, mr_iid: int, title: str | None = None,
              description: str | None = None) -> None:
    """Retitle/redescribe a platform MR - the runner opens it via push options
    with no title (no dynamic text in the entrypoint), so the worker
    personalizes it afterwards (§PR description parity, non-downgrading)."""
    payload = {}
    if title:
        payload["title"] = title
    if description:
        payload["description"] = description
    if not payload:
        return
    with _client() as c:
        r = c.put(f"/projects/{project_id}/merge_requests/{mr_iid}", json=payload)
        r.raise_for_status()


def get_mr(project_id: int, mr_iid: int) -> dict:
    """Fetch one platform MR by iid (its `state` tells the sweep merged vs closed
    vs opened without spinning through the full auto_merge poll)."""
    with _client() as c:
        r = c.get(f"/projects/{project_id}/merge_requests/{mr_iid}")
        r.raise_for_status()
        return r.json()


def _mr_change_summary(c: httpx.Client, base: str, mr_iid: int,
                       max_files: int, max_commits: int) -> dict:
    """Shared body of the platform/customer MR change summaries (§work answers):
    commit subjects + changed paths, never diff content."""
    rc = c.get(f"{base}/merge_requests/{mr_iid}/commits", params={"per_page": max_commits})
    rc.raise_for_status()
    rf = c.get(f"{base}/merge_requests/{mr_iid}/changes")
    rf.raise_for_status()
    changes = rf.json().get("changes", [])[:max_files]
    return {
        "commits": [(cm.get("title") or "")[:200] for cm in rc.json()[:max_commits]],
        "files": [{"path": ch.get("new_path") or ch.get("old_path") or "",
                   "status": ("added" if ch.get("new_file") else
                              "removed" if ch.get("deleted_file") else
                              "renamed" if ch.get("renamed_file") else "modified")}
                  for ch in changes],
    }


def mr_change_summary(project_id: int, mr_iid: int, max_files: int = 40,
                      max_commits: int = 20) -> dict:
    """github.pr_change_summary's platform-GitLab sibling."""
    with _client() as c:
        return _mr_change_summary(c, f"/projects/{project_id}", mr_iid, max_files, max_commits)


def auto_merge(project_id: int, mr_iid: int, timeout_s: int = 300,
               squash: bool = True) -> tuple[bool, str]:
    """§14.5 auto-merge-on-green-CI. If the MR has a pipeline, merge only once the
    pipeline FOR THE CURRENT HEAD COMMIT passes; if there is no pipeline, merge
    directly. Squashes the agent's commits into one by default. Returns (merged,
    reason). SHA-aware so a re-pushed fix isn't judged by the previous commit's
    stale (failed) pipeline."""
    deadline = time.time() + timeout_s
    no_ci_grace = time.time() + 45  # if no pipeline appears by now, assume no CI
    CONFLICT = {"conflict", "broken_status", "discussions_not_resolved",
                "not_approved", "requested_changes"}

    def get_mr(c):
        return c.get(f"/projects/{project_id}/merge_requests/{mr_iid}").json()

    def pipeline_for(c, sha):
        # MR.head_pipeline is unreliable for branch pipelines; look it up by SHA.
        ps = c.get(f"/projects/{project_id}/pipelines", params={"sha": sha}).json()
        return ps[0] if ps else None

    def try_merge(c, mwps=True):
        data = {"squash": True} if squash else {}
        if mwps:
            data["merge_when_pipeline_succeeds"] = True
        r = c.put(f"/projects/{project_id}/merge_requests/{mr_iid}/merge", data=data)
        return r.status_code == 200 and r.json().get("state") == "merged"

    with _client() as c:
        while time.time() < deadline:
            mr = get_mr(c)
            if mr.get("state") == "merged":
                return True, "merged"
            detailed = mr.get("detailed_merge_status") or mr.get("merge_status")
            if detailed in CONFLICT:
                return False, "merge conflict - needs manual resolution"

            pipe = pipeline_for(c, mr.get("sha")) or {}
            status = pipe.get("status")
            if status in ("failed", "canceled"):
                return False, "ci_failed"
            if status == "success":
                if try_merge(c):
                    return True, "merged after green CI"
            elif not pipe and time.time() > no_ci_grace:
                # No pipeline ever appeared - repo has no CI; merge directly.
                if try_merge(c, mwps=False):
                    return True, "merged"
            elif pipe:
                # running/pending → let GitLab merge when it goes green
                c.put(f"/projects/{project_id}/merge_requests/{mr_iid}/merge",
                      data={"merge_when_pipeline_succeeds": True,
                            **({"squash": True} if squash else {})})
            time.sleep(5)
        return False, "ci_timeout"


def failed_pipeline_logs(project_id: int, mr_iid: int, max_chars: int = 6000) -> str:
    """Concatenated traces of the failed jobs on the MR's head pipeline, for
    feeding back to the agent as a fix task."""
    with _client() as c:
        mr = c.get(f"/projects/{project_id}/merge_requests/{mr_iid}").json()
        ps = c.get(f"/projects/{project_id}/pipelines", params={"sha": mr.get("sha")}).json()
        if not ps:
            return ""
        pid = ps[0]["id"]
        jobs = c.get(f"/projects/{project_id}/pipelines/{pid}/jobs").json()
        out = []
        for j in jobs:
            if j.get("status") != "failed":
                continue
            trace = c.get(f"/projects/{project_id}/jobs/{j['id']}/trace").text
            out.append(f"### job '{j['name']}' failed\n{trace[-max_chars:]}")
        return "\n\n".join(out)[-max_chars:]


# ---------------------------------------------------------------- customer GitLab
# A customer's own GitLab repo (gitlab.com or self-hosted), authenticated with a
# per-project GITLAB_TOKEN Memory secret - NOT the platform token/host. The
# GitLab sibling of github.py: seed the base branch, open/inspect/merge the MR
# and read its diff for the §14.7 security review. Every call takes base_url +
# token so it never touches the platform GitLab. Failures raise GitLabError.

def _customer_client(base_url: str, token: str) -> httpx.Client:
    if not token:
        raise GitLabError("GITLAB_TOKEN not configured")
    return httpx.Client(
        base_url=f"{base_url.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": token},
        timeout=30,
    )


def check_repo_access(base_url: str, path: str, token: str) -> tuple[bool, str]:
    """Auth check: GET /projects/:urlencoded-path with the customer PAT confirms
    the token is valid AND can access THAT repo. Returns (ok, human detail). The
    GitLab sibling of github.check_repo_access - gates saving auto_merge."""
    if not token:
        return False, "Add a GITLAB_TOKEN secret in Memory first."
    try:
        with _customer_client(base_url, token) as c:
            r = c.get(f"/projects/{quote(path, safe='')}")
    except httpx.HTTPError as exc:
        return False, f"Could not reach GitLab: {exc}"
    if r.status_code == 200:
        name = r.json().get("path_with_namespace", path)
        return True, f"Authenticated - access to {name} confirmed."
    if r.status_code in (401, 403):
        return False, "The GITLAB_TOKEN is invalid or lacks access to this repository."
    if r.status_code == 404:
        return False, "Repository not found, or the token can't see it (needs api scope)."
    return False, f"GitLab returned {r.status_code}."


def customer_ensure_base(base_url: str, token: str, path: str, branch: str = "main") -> None:
    """Seed the base branch on an empty customer repo so an MR has a target.
    Idempotent: no-op once the branch exists."""
    with _customer_client(base_url, token) as c:
        pid = quote(path, safe="")
        r = c.get(f"/projects/{pid}/repository/branches/{quote(branch, safe='')}")
        if r.status_code == 200:
            return
        r = c.post(f"/projects/{pid}/repository/commits", json={
            "branch": branch,
            "commit_message": f"{settings.brand_name}: initialise repository",
            "actions": [{"action": "create", "file_path": "README.md",
                         "content": f"# {path}\n\nManaged by {settings.brand_name}. "
                                    "The MVP lands here via merge request.\n"}]})
        if r.status_code not in (200, 201):
            raise GitLabError(f"seed base branch failed: {r.status_code} {r.text[:200]}")
        log.info("seeded customer %s base branch %s", path, branch)


def customer_find_open_mr(base_url: str, token: str, path: str,
                          source_branch: str) -> dict | None:
    with _customer_client(base_url, token) as c:
        r = c.get(f"/projects/{quote(path, safe='')}/merge_requests",
                  params={"state": "opened", "source_branch": source_branch})
        r.raise_for_status()
        mrs = r.json()
        return mrs[0] if mrs else None


def customer_open_mr(base_url: str, token: str, path: str, source_branch: str,
                     target_branch: str, title: str, body: str) -> dict:
    """Open an MR source→target, or return the existing open one. Returns the MR
    JSON ({iid, web_url, ...})."""
    existing = customer_find_open_mr(base_url, token, path, source_branch)
    if existing:
        return existing
    with _customer_client(base_url, token) as c:
        r = c.post(f"/projects/{quote(path, safe='')}/merge_requests", json={
            "source_branch": source_branch, "target_branch": target_branch,
            "title": title, "description": body})
        if r.status_code in (200, 201):
            return r.json()
        again = customer_find_open_mr(base_url, token, path, source_branch)
        if again:
            return again
        raise GitLabError(f"open MR failed: {r.status_code} {r.text[:200]}")


def customer_update_mr_desc(base_url: str, token: str, path: str, iid: int,
                            description: str) -> None:
    """Replace an open MR's description (§PR description parity: a revise run's
    fresh pr.md must reach the displayed description - customer_open_mr returns
    a pre-existing MR untouched)."""
    with _customer_client(base_url, token) as c:
        r = c.put(f"/projects/{quote(path, safe='')}/merge_requests/{iid}",
                  json={"description": description})
        r.raise_for_status()


def customer_list_open_issues(base_url: str, token: str, path: str) -> list[dict]:
    """Open issues normalized for the §auto_dev sweep (same shape as
    github.list_open_issues; GitLab labels are already plain strings)."""
    with _customer_client(base_url, token) as c:
        r = c.get(f"/projects/{quote(path, safe='')}/issues",
                  params={"state": "opened", "per_page": 100})
        r.raise_for_status()
        return [{
            "iid": it["iid"],
            "url": it.get("web_url", ""),
            "title": it.get("title", ""),
            "body": it.get("description") or "",
            "labels": it.get("labels", []) or [],
            "assignees": [a.get("username", "") for a in it.get("assignees", []) or []],
            "author": (it.get("author") or {}).get("username", ""),
        } for it in r.json()]


def customer_upload_file(base_url: str, token: str, path: str,
                         filename: str, data: bytes) -> str:
    """Upload a file to the project and return GitLab's ready-made markdown
    (`![...](/uploads/...)`) - the §After-shots hosting primitive."""
    with _customer_client(base_url, token) as c:
        r = c.post(f"/projects/{quote(path, safe='')}/uploads",
                   files={"file": (filename, data, "image/png")})
        r.raise_for_status()
        return r.json()["markdown"]


def customer_create_mr_note(base_url: str, token: str, path: str,
                            mr_iid: int, body: str) -> None:
    with _customer_client(base_url, token) as c:
        r = c.post(f"/projects/{quote(path, safe='')}/merge_requests/{mr_iid}/notes",
                   json={"body": body})
        r.raise_for_status()


def customer_create_issue_note(base_url: str, token: str, path: str,
                               issue_iid: int, body: str) -> None:
    with _customer_client(base_url, token) as c:
        r = c.post(f"/projects/{quote(path, safe='')}/issues/{issue_iid}/notes",
                   json={"body": body})
        r.raise_for_status()


def customer_get_mr(base_url: str, token: str, path: str, mr_iid: int) -> dict:
    with _customer_client(base_url, token) as c:
        r = c.get(f"/projects/{quote(path, safe='')}/merge_requests/{mr_iid}")
        r.raise_for_status()
        return r.json()


def customer_mr_change_summary(base_url: str, token: str, path: str, mr_iid: int,
                               max_files: int = 40, max_commits: int = 20) -> dict:
    """mr_change_summary against a CUSTOMER GitLab host."""
    with _customer_client(base_url, token) as c:
        return _mr_change_summary(c, f"/projects/{quote(path, safe='')}", mr_iid,
                                  max_files, max_commits)


def customer_branch_exists(base_url: str, token: str, path: str, branch: str) -> bool:
    """Whether the branch still exists on the customer repo (§branch naming)."""
    with _customer_client(base_url, token) as c:
        r = c.get(f"/projects/{quote(path, safe='')}/repository/branches/{quote(branch, safe='')}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True


def customer_branch_ahead(base_url: str, token: str, path: str, branch: str,
                          base: str) -> bool:
    """Whether `branch` carries commits `base` does not, on a customer GitLab
    (§14 resume-publish) - the github.branch_ahead_of_base sibling. A 404 with
    the branch known to exist means the base was never born (uninitialized
    repo), which makes everything on the branch unpublished: True."""
    with _customer_client(base_url, token) as c:
        r = c.get(f"/projects/{quote(path, safe='')}/repository/compare",
                  params={"from": base, "to": branch})
        if r.status_code == 404:
            return True
        if r.status_code != 200:
            raise GitLabError(f"branch compare failed: {r.status_code} {r.text[:200]}")
        return bool(r.json().get("commits"))


def customer_mr_diff(base_url: str, token: str, path: str, mr_iid: int) -> str:
    """Unified diff of the MR (per-file diffs with +++ headers) for the §14.7
    security review - the GitLab sibling of github.pr_diff. The deterministic
    floor scans the '+' added lines, so the +++/--- headers matter."""
    with _customer_client(base_url, token) as c:
        r = c.get(f"/projects/{quote(path, safe='')}/merge_requests/{mr_iid}/changes")
        if r.status_code != 200:
            raise GitLabError(f"mr diff failed: {r.status_code} {r.text[:200]}")
        changes = r.json().get("changes", [])
    out = []
    for ch in changes:
        out.append(f"--- a/{ch.get('old_path', '')}\n+++ b/{ch.get('new_path', '')}\n"
                   f"{ch.get('diff', '')}")
    return "\n".join(out)


def customer_merge_mr(base_url: str, token: str, path: str, mr_iid: int,
                      squash: bool = True) -> tuple[bool, str]:
    """Direct merge of a customer MR (the §14.7 auto-merge path merges only after
    a clean security review), squashed by default. Falls back to a plain merge
    when the customer's project forbids squashing (better merged un-squashed than
    parked). Returns (merged, reason)."""
    with _customer_client(base_url, token) as c:
        url = f"/projects/{quote(path, safe='')}/merge_requests/{mr_iid}/merge"
        r = c.put(url, json={"squash": True} if squash else {})
        if r.status_code == 200 and r.json().get("state") == "merged":
            return True, "merged"
        if squash and r.status_code in (400, 405, 422):
            r = c.put(url, json={})
            if r.status_code == 200 and r.json().get("state") == "merged":
                return True, "merged (project does not allow squash)"
        return False, f"merge blocked: {r.status_code} {r.text[:200]}"
