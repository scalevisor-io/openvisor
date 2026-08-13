"""Deterministic sovereign-technology gate (§Phase 2 - the speciality moat).

A sovereign project's sovereignty must be VERIFIABLE, not prose in a prompt. This
scans the built workspace for US-hyperscaler SDKs, container registries and
managed-service endpoints that a self-hostable, EU-sovereign, hosting-neutral
deliverable must not depend on, and fails the run when a `sovereign` project uses
them - the same deterministic-floor posture as the leak scan. The dev prompt's
{{SOVEREIGN_CLAUSE}} ASKS the agent to avoid them; this ENFORCES it, which is what
turns the sovereign speciality into a defensible product claim rather than
marketing. It keys on project.sovereign (a customer choice), NEVER on the
speciality - the aws/gcp/azure tracks intentionally use those technologies.

Scope (stated honestly - the product claim is "detects common US-hyperscaler SDKs,
registries and managed endpoints in supported ecosystems", not a proof of absence):
markers are high-precision (specific package names, registries, managed endpoints -
never generic words) so a legitimate EU stack is not false-flagged; coverage is the
Python/JS/Go/Ruby manifests + source + infra/config files below (Java/.NET/Rust and
vendored/compiled SDKs are out of scope for now).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# The self-contained prefix every sovereign fix message starts with, so the run
# loop can tell a sovereign failure apart from a boot failure (own message + park).
SOVEREIGN_FIX_PREFIX = "This project is SOVEREIGN:"

_CATEGORIES = {
    "AWS": [r"\bboto3\b", r"\bbotocore\b", r"\baws-sdk\b", r"@aws-sdk/", r"\bawscli\b",
            r"amazonaws\.com", r"\.dkr\.ecr\.", r"AWS_SECRET_ACCESS_KEY"],
    # NOT bare googleapis.com - fonts.googleapis.com / maps.googleapis.com are in a
    # large fraction of legit EU frontends and are not the Cloud SDK.
    "Google Cloud": [r"google-cloud-[a-z]", r"@google-cloud/", r"\bgcr\.io\b",
                     r"firebaseio\.com", r"\bcloudfunctions\.net\b",
                     r"(?:storage|compute|bigquery|run)\.googleapis\.com"],
    "Microsoft Azure": [r"@azure/", r"\bazure-sdk\b", r"\bazure-storage\b", r"azurecr\.io",
                        r"\.blob\.core\.windows\.net", r"cognitiveservices\.azure",
                        r"AZURE_CLIENT_SECRET"],
}
_PATTERNS = [(cat, re.compile(p)) for cat, ps in _CATEGORIES.items() for p in ps]

# Files worth scanning: dependency manifests + source + infra/config. Note
# package-lock.json (a .json) IS scanned; poetry.lock / go.sum / yarn.lock are not.
_SCAN_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".json", ".txt", ".toml",
              ".yml", ".yaml", ".tf", ".go", ".rb", ".sh", ".cfg", ".ini", ".mod"}
_SCAN_NAMES = {"Dockerfile", "requirements.txt", "package.json", "go.mod", "Gemfile",
               "pyproject.toml", "Pipfile", ".env"}
_SKIP_DIRS = {".git", "node_modules", ".openvisor", "__pycache__", "dist", "build",
              ".venv", "venv", ".next", "vendor", ".terraform"}
_MAX_FILE = 512 * 1024
_MAX_FILES = 5000       # bound the walk - a huge agent-produced tree can't stall the worker
_MAX_FINDINGS = 25


def _iter_files(root: Path):
    for p in root.rglob("*"):
        try:
            if p.is_symlink() or not p.is_file():
                continue
            parts = p.relative_to(root).parts
            if any(part in _SKIP_DIRS for part in parts[:-1]):
                continue
            if p.name in _SCAN_NAMES or p.suffix in _SCAN_EXTS:
                yield p
        except OSError:
            continue


def scan_workspace(workdir) -> tuple[list[dict], bool]:
    """Walk the built workspace; return (findings, complete). Each finding is
    {file, category, marker}; findings are deduped + capped. complete=False means
    the scan was truncated (>%d files) or errored and did NOT fully verify - the
    caller must NOT record 'clean' on an incomplete scan (no false product claim).
    Never raises: an unreadable file is skipped; symlinks and non-regular files are
    never followed.""" % _MAX_FILES
    root = Path(workdir)
    findings: list[dict] = []
    seen: set = set()
    complete = True
    count = 0
    try:
        for f in _iter_files(root):
            count += 1
            if count > _MAX_FILES:
                complete = False
                break
            try:
                if f.stat().st_size > _MAX_FILE:
                    continue
                text = f.read_text(errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            rel = str(f.relative_to(root))
            for cat, rx in _PATTERNS:
                m = rx.search(text)
                if not m:
                    continue
                key = (rel, cat, m.group(0))
                if key in seen:
                    continue
                seen.add(key)
                findings.append({"file": rel, "category": cat, "marker": m.group(0)})
                if len(findings) >= _MAX_FINDINGS:
                    return findings, complete
    except Exception as exc:  # noqa: BLE001
        log.warning("sovereign scan incomplete for %s: %s", workdir, exc)
        complete = False
    return findings, complete


def fix_instruction(findings: list[dict]) -> str:
    """A scoped fix instruction for the runner (same shape as boot-fix): replace
    ONLY the non-sovereign dependencies, keep everything working."""
    lines = [f"- {f['file']}: {f['category']} ({f['marker']})" for f in findings[:15]]
    return (
        "This project is SOVEREIGN: it must use ONLY EU-sovereign, self-hostable, "
        "hosting-neutral technologies - no US-hyperscaler SDKs, registries or managed "
        "services. The build currently depends on the following non-sovereign "
        "technologies. Replace each with a self-hostable / European alternative "
        "(e.g. MinIO or local disk instead of S3, a self-hosted Postgres instead of "
        "RDS / Cloud SQL, a European or self-hosted registry instead of ECR / GCR / "
        "ACR), keeping everything working - do NOT change anything else:\n\n"
        + "\n".join(lines))
