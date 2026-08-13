"""Worker-side leak scanning shared by the dev pipeline and program runs.

Two jobs live here:
  - the KB verbatim-span fingerprinting that seeds the dev runner's pre-publish
    scan (moved from workers/tasks.py so program runs reuse the exact logic);
  - the §28 output scan applied to the files a program run would return to the
    customer (output/ + the run log) BEFORE they are shown or webhooked. Same
    semantics as runner/leak_scan.py: verbatim secret values and verbatim
    knowledge-base spans block the run; matched values are never echoed; the
    caller fails OPEN on an internal scanner error (defence in depth - the
    sandbox and prompt rules remain the primary controls).
"""
import logging
import re
from pathlib import Path

from app.core.config import settings
from app.services import meili

log = logging.getLogger(__name__)

# Leak-scan tuning: the min verbatim knowledge-base span (in whitespace-normalized
# characters) treated as an exfiltration attempt, and the sliding-window step.
KB_WINDOW = 200
KB_STEP = 100
MAX_FILE_BYTES = 2_000_000
MIN_SECRET_LEN = 8

# PEM private-key header, any flavor (RSA/EC/DSA/OPENSSH/ENCRYPTED/PGP ... BLOCK) -
# value-independent, so it also blocks keys the scanner holds no copy of
# (runner/leak_scan.py parity).
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _spans(text: str, seen: set[str], allow: str = "") -> list[str]:
    out: list[str] = []
    norm = norm_ws(text)
    for i in range(0, max(0, len(norm) - KB_WINDOW) + 1, KB_STEP):
        win = norm[i:i + KB_WINDOW]
        if len(win) < KB_WINDOW or win in seen or (allow and win in allow):
            continue
        seen.add(win)
        out.append(win)
    return out


def kb_fingerprints(snippets: list[str], allow_text: str) -> list[str]:
    """Turn the KB snippets injected into a dev task into verbatim-span fingerprints
    the runner's leak scan matches against the files it would publish. Windows that
    also occur in `allow_text` (the system prompt + project context, i.e. material
    the agent may legitimately reproduce - e.g. the mandated .gitlab-ci.yml template
    or text the customer themselves supplied) are dropped to avoid false positives."""
    allow = norm_ws(allow_text)
    fps: list[str] = []
    seen: set[str] = set()
    for snip in snippets:
        fps.extend(_spans(snip, seen, allow))
    return fps


def kb_fingerprints_from_db(cap: int = 20000) -> list[str]:
    """Fingerprints over the WHOLE ingested knowledge base (the Meilisearch 'kb'
    index), for program-run output scanning - programs get no injected snippets, so
    any KB text in their output is exfiltration. Capped for worker-side memory/CPU; a
    hit on the cap logs loudly (the scan then covers a KB prefix only)."""
    fps: list[str] = []
    seen: set[str] = set()
    for content in meili.all_kb_docs():
        for win in _spans(content or "", seen):
            fps.append(win)
            if len(fps) >= cap:
                log.warning("KB fingerprints capped at %d - program output scan "
                            "covers a knowledge-base prefix only", cap)
                return fps
    return fps


def platform_secret_values(extra_values: list[str] | None = None,
                           ssh_private_keys: list[str] | None = None) -> list[str]:
    """Verbatim values that must never appear in a program run's output: the
    platform model/embedding/context7 keys its env file carried, the GitLab
    token, any per-program API key, and the instance SSH private-key material
    (base64 body lines, not the PEM markers - runner/leak_scan.py parity)."""
    vals = [settings.openai_api_key, settings.embedding_api_key,
            settings.context7_api_key, settings.gitlab_token,
            *(extra_values or [])]
    for pem in ssh_private_keys or []:
        for ln in (pem or "").splitlines():
            ln = ln.strip()
            if len(ln) >= 40 and "PRIVATE KEY" not in ln:
                vals.append(ln)
    seen: set[str] = set()
    out: list[str] = []
    for v in vals:
        v = (v or "").strip()
        if len(v) >= MIN_SECRET_LEN and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def read_scannable(p: Path) -> str | None:
    """File content for scanning, or None for binaries/oversized/unreadable
    files (same skip rules as runner/leak_scan.py)."""
    try:
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            return None
        data = p.read_bytes()
        if b"\x00" in data:  # binary
            return None
        return data.decode("utf-8", "replace")
    except OSError:
        return None


def scan_output(root: Path, files: list[Path], log_text: str,
                secrets: list[str], fingerprints: list[str]) -> list[str]:
    """Findings ("<path>: <category>") for output files or the run log that
    contain a secret value or a verbatim KB span. Empty list = clean. Values
    are never included - only the path and the category of the match."""
    findings: list[str] = []
    for p in files:
        content = read_scannable(p)
        if content is None:
            continue
        rel = p.relative_to(root)
        if PRIVATE_KEY_RE.search(content):
            findings.append(f"{rel}: contains private-key material (PEM header)")
            continue
        if any(sv in content for sv in secrets):
            findings.append(f"{rel}: contains a secret value (redacted)")
            continue
        norm = norm_ws(content)
        if any(fp in norm for fp in fingerprints):
            findings.append(f"{rel}: contains verbatim knowledge-base text")
    if log_text:
        if PRIVATE_KEY_RE.search(log_text):
            findings.append("run log: contains private-key material (PEM header)")
        elif any(sv in log_text for sv in secrets):
            findings.append("run log: contains a secret value (redacted)")
        elif any(fp in norm_ws(log_text) for fp in fingerprints):
            findings.append("run log: contains verbatim knowledge-base text")
    return sorted(set(findings))
