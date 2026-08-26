"""Git knowledge sources (§KB): clone/refresh every enabled+verified git
KnowledgeBase into /workspaces/.kb-git/<kb-id> so the ingest task can fold those
trees into the local KB reindex (services/rag multi-root ingest).

Transport hygiene: SSH sources write the decrypted deploy key to a 0600 temp file
consumed via GIT_SSH_COMMAND (never an argv/env leak). HTTP sources pass the PAT as
an ephemeral `http.extraHeader` Basic credential via git's GIT_CONFIG_* env (NOT in
the URL and NOT in argv), so the token is never written into `.git/config` on the
shared workspaces volume and the persisted `origin` is always the clean URL. Errors
are redacted (repos.redact_secret) before they reach last_index_error/logs, and a
subprocess timeout is caught so its argv-bearing message never propagates. A
per-source failure is recorded on the row and skipped - one bad repo never aborts
the reindex of the others."""
import base64
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption import decrypt
from app.models import KnowledgeBase
from app.models.models import utcnow
from app.services import rag, repos

log = logging.getLogger(__name__)

# Cloned git-KB checkouts live under the workspaces volume (mounted read-write in
# the worker), in a dedicated subtree the per-project dev sandboxes never mount.
KB_GIT_ROOT = Path(settings.workspaces_dir) / ".kb-git"

_SSH_OPTS = ("-o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
             "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=10")
_GIT_TIMEOUT = 120  # seconds per git command (shallow clone/fetch of a KB repo)


def clone_dir(kb_id: str) -> Path:
    return KB_GIT_ROOT / kb_id


def _run(cmd: list[str], env: dict, *secrets: str) -> None:
    """Run a git command, raising RuntimeError with a secret-scrubbed message on
    failure so a credential never reaches last_index_error/logs. A timeout is caught
    here: `TimeoutExpired.__str__` embeds the full argv, so we raise a generic message
    instead of letting it propagate."""
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {_GIT_TIMEOUT}s") from None
    if proc.returncode != 0:
        err = repos.redact_secret((proc.stderr or proc.stdout or "").strip(), *secrets)
        detail = err.splitlines()[-1].strip() if err else f"exit {proc.returncode}"
        raise RuntimeError(detail)


def _http_auth_env(env: dict, pat: str, username: str | None = None) -> str:
    """Add an ephemeral `http.extraHeader: Authorization: Basic …` to `env` via git's
    GIT_CONFIG_* protocol (out of argv and never persisted to .git/config). Returns
    the base64 credential so the caller can pass it to _run for redaction."""
    # <username>:<pat> - token as password. Default username oauth2 fits GitHub and
    # GitLab PATs (matches repos.https_with_pat); a GitLab deploy token or Bitbucket
    # app password carries its own required username.
    user = (username or "").strip() or "oauth2"
    cred = base64.b64encode(f"{user}:{pat}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {cred}"
    return cred


def sync_source(kb: KnowledgeBase) -> Path:
    """Clone (first time) or shallow-refresh a git KB into its checkout dir and
    return it. Always uses the clean URL (no embedded credential); an HTTP PAT rides
    as an ephemeral extraHeader. Raises on any git/transport failure."""
    dest = clone_dir(kb.id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    ref = (kb.ref or "main").strip() or "main"
    uri = repos.normalize_ssh_uri(kb.uri)  # scp-like host:port/path dials port 22 otherwise
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    secrets: tuple[str, ...] = ()
    keydir: tempfile.TemporaryDirectory | None = None

    if kb.auth_kind == "ssh":
        key = (decrypt(kb.ssh_private_key_enc) if kb.ssh_private_key_enc else "").strip()
        if not key:
            raise RuntimeError("no deploy key stored for this source")
        keydir = tempfile.TemporaryDirectory()
        keyfile = Path(keydir.name) / "id"
        keyfile.write_text(key + "\n")
        keyfile.chmod(0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {keyfile} {_SSH_OPTS}"
    else:  # http
        pat = (decrypt(kb.api_key_enc) if kb.api_key_enc else "").strip()
        if not pat:
            raise RuntimeError("no access token stored for this source")
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
        cred = _http_auth_env(env, pat, kb.http_username)
        secrets = (pat, cred)  # redact both the raw PAT and its base64 form

    # §ssh remotes: a KB on a tailnet-only git host needs the same transport
    # mapping the worker uses for project repos - this pod has no hostAlias.
    rw = repos.git_host_rewrite(uri)
    try:
        if (dest / ".git").is_dir():
            _run(["git", *rw, "-C", str(dest), "fetch", "--depth", "1", uri, ref], env, *secrets)
            _run(["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"], env, *secrets)
            # Belt-and-braces: ensure the persisted origin carries no credential (e.g.
            # a checkout left by an older code path that embedded the PAT in the URL).
            if kb.auth_kind == "http":
                _run(["git", "-C", str(dest), "remote", "set-url", "origin", uri], env, *secrets)
        else:
            if dest.exists():
                shutil.rmtree(dest)
            _run(["git", *rw, "clone", "--depth", "1", "--branch", ref, uri, str(dest)], env, *secrets)
    finally:
        if keydir is not None:
            keydir.cleanup()
    return dest


def prepare_roots(db: Session) -> tuple[list[tuple[str, Path]], int]:
    """Build the (root_key, path) roots the reindex folds together: the local
    /knowledge tree (root_key 'local', only when the local KB row is enabled) plus one
    per enabled+verified git source (root_key = kb id) after cloning/refreshing it.
    A git source that fails to sync records last_index_error and is skipped; the rest
    proceed. Returns (roots, error_count) - the caller uses error_count to avoid
    swapping the live index to empty when every active source transiently failed.
    Commits the per-source status updates; secrets are scrubbed from any error text."""
    roots: list[tuple[str, Path]] = []
    if rag.local_kb_enabled(db):
        roots.append(("local", rag.KNOWLEDGE_ROOT))
    git_kbs = db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.kind == "git",
            KnowledgeBase.enabled.is_(True),
            KnowledgeBase.verified.is_(True),
        ).order_by(KnowledgeBase.id)  # deterministic root order for the fingerprint
    ).scalars().all()
    errors = 0
    for kb in git_kbs:
        try:
            dest = sync_source(kb)
            kb.last_indexed_at = utcnow()
            kb.last_index_error = None
            roots.append((kb.id, dest))
        except Exception as exc:  # one bad repo never aborts the others
            errors += 1
            # Defence in depth: strip any credential/userinfo the exception text
            # might carry before persisting it to an API-exposed column / the logs.
            safe = repos.redact_secret(str(exc))
            kb.last_index_error = safe[:2000]
            log.warning("kb-git sync failed for %s (%s): %s", kb.id, kb.name, safe)
    db.commit()
    return roots, errors
