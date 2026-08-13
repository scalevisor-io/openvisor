"""Openvisor pre-publish leak scan - the hard boundary behind the dev prompt's
confidentiality rules (development_system.md rules 9-11).

Runs inside the dev runner after the agent finishes and after `git add -A`, but
BEFORE the commit/push. It scans what this run would publish for material that must
never reach the customer's repository:

  - any file under .openvisor/ - the runner's own inputs (task, deploy key, KB
    fingerprints). They are supposed to be gitignored and unstaged before the
    scan runs, so one showing up in the index means that plumbing failed and
    publishing would ship the key;
  - PRIVATE-KEY material by PATTERN: any staged file carrying a PEM private-key
    header (OpenSSH/RSA/EC/PGP/...), whatever the key is - value-independent, so
    it also catches keys the scanner has no copy of;
  - verbatim SECRET values (and their base64/hex encodings): the secret project-
    Memory entries (looked up by name in the environment the entrypoint exported),
    the platform model API key (LLM_API_KEY), the git deploy key material, and the
    connected MCP-KB / Context7 API keys the worker embedded in mcp.json (values in
    leak_extra_secrets.txt); and
  - verbatim spans of the platform KNOWLEDGE BASE: the RAG snippets injected into
    task.md, pre-fingerprinted by the worker (leak_kb.json), with anything the
    agent may legitimately reproduce already filtered out.

The same checks run over three surfaces: the staged files, the MESSAGES of the
commits this run would push, and the ADDED lines of those commits (so a secret
committed then removed from the working tree - still recoverable from the pushed
history - is caught, not just what the final snapshot shows). The commit surfaces
are scanned only when remote-tracking refs exist (the runner has fetched origin),
so the range HEAD --not --remotes is exactly this run's new commits.

Any hit exits non-zero so the entrypoint refuses to commit/push and the run is
reported as a failure (fail closed). Secret values and KB text are NEVER printed -
only the offending surface and the category of match. KB scanning can be turned off
with LEAK_SCAN_KB=0 (secret scanning is always on).

Scope note: this catches verbatim copying and simple base64/hex encodings of KNOWN
secret values (the realistic prompt-injection cases). It does not defeat arbitrary
obfuscation of KB prose (paraphrase, rot13, split strings) - the prompt rules remain
the primary control and this is defence in depth. On an INTERNAL scanner error it
fails OPEN by default (a scanner bug must not brick every build); set
LEAK_SCAN_FAIL_CLOSED=1 to block the publish on a scanner error instead.
"""
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path("/workspace")
OPENVISOR = WORKSPACE / ".openvisor"

MAX_FILE_BYTES = 2_000_000
MAX_COMMIT_SCAN_BYTES = 5_000_000  # cap the patch text scanned, huge diffs aside
MIN_SECRET_LEN = 8

# PEM private-key header, any flavor (RSA/EC/DSA/OPENSSH/ENCRYPTED/PGP ... BLOCK).
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(WORKSPACE), *args],
                         capture_output=True, text=True)
    return out.stdout


def _staged_files() -> list[Path]:
    """Files this run would publish: added/copied/modified in the index."""
    files = []
    for line in _git("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines():
        line = line.strip()
        if line:
            files.append(WORKSPACE / line)
    return files


def _read_text(p: Path) -> str | None:
    try:
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            return None
        data = p.read_bytes()
        if b"\x00" in data:  # binary
            return None
        return data.decode("utf-8", "replace")
    except OSError:
        return None


def _secret_values() -> list[str]:
    vals: list[str] = []
    # 1. secret Memory values: names from the worker, values from the environment.
    # The worker writes this file on EVERY dispatch (empty when the project has no
    # secrets), so a missing file means the agent deleted the .openvisor/ inputs
    # mid-run - an internal error (main()'s fail-open/closed policy), never a
    # silent scan with a reduced refuse-set.
    names_file = OPENVISOR / "leak_secret_env.txt"
    if not names_file.exists():
        raise RuntimeError("leak_secret_env.txt missing - .openvisor/ inputs were deleted mid-run")
    for name in names_file.read_text().splitlines():
        name = name.strip()
        if name and os.environ.get(name):
            vals.append(os.environ[name])
    # 2. the platform model API key
    if os.environ.get("LLM_API_KEY"):
        vals.append(os.environ["LLM_API_KEY"])
    # 3. extra secret VALUES the worker embedded in .openvisor/ inputs (the connected
    # MCP-KB / Context7 API keys written into mcp.json) - raw values, one per line.
    extra_file = OPENVISOR / "leak_extra_secrets.txt"
    if extra_file.exists():
        for ln in extra_file.read_text().splitlines():
            ln = ln.strip()
            if ln:
                vals.append(ln)
    # 4. the git deploy key material (base64 body lines, not the PEM markers)
    key = Path.home() / ".ssh" / "id_ed25519"
    if key.exists():
        try:
            for ln in key.read_text().splitlines():
                ln = ln.strip()
                if len(ln) >= 40 and "PRIVATE KEY" not in ln:
                    vals.append(ln)
        except OSError:
            pass
    seen, out = set(), []
    for v in vals:
        v = v.strip()
        if len(v) >= MIN_SECRET_LEN and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _secret_variants(secrets: list[str]) -> list[str]:
    """Each secret plus its base64 / urlsafe-base64 (padded and unpadded) and hex
    encodings, so an agent that base64/hex-encodes a known secret before writing it
    into a file/commit is still caught. Only forms >= MIN_SECRET_LEN are kept, so no
    short encoding turns into a false-positive substring."""
    out: set[str] = set()
    for sv in secrets:
        out.add(sv)
        try:
            b = sv.encode()
            b64 = base64.b64encode(b).decode()
            u64 = base64.urlsafe_b64encode(b).decode()
            out.update({b64, b64.rstrip("="), u64, u64.rstrip("="), b.hex()})
        except Exception:
            pass
    return [v for v in out if len(v) >= MIN_SECRET_LEN]


def _kb_fingerprints() -> list[str]:
    if os.environ.get("LEAK_SCAN_KB", "1") == "0":
        return []
    # Written unconditionally by the worker (an empty list when no KB is active),
    # so absence = the agent deleted the .openvisor/ inputs mid-run - internal
    # error, same policy as the secret-names file above.
    f = OPENVISOR / "leak_kb.json"
    if not f.exists():
        raise RuntimeError("leak_kb.json missing - .openvisor/ inputs were deleted mid-run")
    try:
        fps = json.loads(f.read_text())
        return [s for s in fps if isinstance(s, str) and s]
    except (OSError, ValueError):
        return []


def _scan_blob(label: str, content: str, secrets: list[str], kb: list[str]) -> str | None:
    """Run the secret / private-key / KB checks over one text blob. Returns a finding
    string or None. `secrets` are the encoding VARIANTS from _secret_variants."""
    if PRIVATE_KEY_RE.search(content):
        return f"{label}: contains private-key material (PEM header)"
    if any(sv in content for sv in secrets):
        return f"{label}: contains a secret value (redacted)"
    if any(fp in _norm(content) for fp in kb):
        return f"{label}: contains verbatim knowledge-base text"
    return None


def _has_remotes() -> bool:
    """Whether remote-tracking refs exist - the runner has fetched origin, so
    HEAD --not --remotes is exactly this run's new commits. Without remotes the same
    range would list the whole history, so the commit surfaces are skipped."""
    return bool(_git("for-each-ref", "refs/remotes").strip())


def _new_commit_messages() -> str:
    return _git("log", "--format=%B", "HEAD", "--not", "--remotes")


def _new_commit_added_lines() -> str:
    """Added lines (leading '+', excluding '+++' headers) across this run's new
    commits - what the agent introduced, ignoring pre-existing context/base content
    to avoid flagging the customer's own repo. Capped for very large diffs."""
    patch = _git("log", "--format=", "-p", "--no-color", "HEAD", "--not", "--remotes")
    if len(patch) > MAX_COMMIT_SCAN_BYTES:
        patch = patch[:MAX_COMMIT_SCAN_BYTES]
    return "\n".join(ln[1:] for ln in patch.splitlines()
                     if ln.startswith("+") and not ln.startswith("+++"))


def _scan() -> int:
    files = _staged_files()
    secrets = _secret_variants(_secret_values())
    kb = _kb_fingerprints()

    findings: list[str] = []
    for p in files:
        rel = p.relative_to(WORKSPACE)
        if ".openvisor" in rel.parts:
            # Runner-internal inputs (deploy key, task, KB fingerprints) must never
            # be published; the entrypoint gitignores + unstages them, so one still
            # staged means that plumbing failed. Block regardless of content.
            findings.append(f"{rel}: runner-internal .openvisor/ file staged for publish")
            continue
        content = _read_text(p)
        if content is None:
            continue
        f = _scan_blob(str(rel), content, secrets, kb)
        if f:
            findings.append(f)

    # Agent-authored commits: the staged snapshot doesn't show a secret committed
    # then reverted, nor anything hidden in a commit message. Both would still be
    # pushed, so scan them too (only when remote refs bound the range).
    if _has_remotes():
        msgs = _new_commit_messages()
        if msgs.strip():
            f = _scan_blob("a commit message in this build", msgs, secrets, kb)
            if f:
                findings.append(f)
        added = _new_commit_added_lines()
        if added.strip():
            f = _scan_blob("this build's commit history", added, secrets, kb)
            if f:
                findings.append(f)

    if findings:
        print("leak-scan: LEAK_SCAN_BLOCKED - exfiltration detected in content to be "
              "published:", file=sys.stderr)
        for f in sorted(set(findings)):
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"leak-scan: OK - {len(files)} staged file(s) + commit history clear "
          f"({len(secrets)} secret form(s) / {len(kb)} KB span(s) checked)")
    return 0


def main() -> int:
    try:
        return _scan()
    except Exception as exc:  # noqa: BLE001
        # An INTERNAL error (not a detected leak) fails OPEN by default: this is a
        # secondary guard behind the prompt rules, and a scanner bug must not brick
        # every build. LEAK_SCAN_FAIL_CLOSED=1 flips it to block on a scanner error.
        # A detected leak still returns 1 above. Loud so it gets noticed.
        fail_closed = os.environ.get("LEAK_SCAN_FAIL_CLOSED", "0") == "1"
        verb = "blocking" if fail_closed else "allowing"
        print(f"leak-scan: internal error, {verb} publish: {exc}", file=sys.stderr)
        return 1 if fail_closed else 0
    finally:
        for name in ("leak_secret_env.txt", "leak_kb.json", "leak_extra_secrets.txt"):
            try:
                (OPENVISOR / name).unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
