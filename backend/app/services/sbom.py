"""DevSecOps SBOM + CVE gate (§Phase 2 - the devsecops-hardened overlay's deterministic
gate, sibling of services/sovereign.py).

The deployer generates the SBOM and scans it with trivy (platform-side, so the agent
can't fake it); THIS builds the scoped fix instruction and the pass/fail decision. A
DevSecOps build that ships a CRITICAL known-CVE dependency is BLOCKED - the prompt asks
the agent to keep dependencies current; this enforces it. HIGH findings are advisory
(recorded, surfaced, never gate). The message is self-labeled (SBOM_FIX_PREFIX) so the
run loop parks/labels it as a security-scan failure, not a boot failure.

Continuous post-delivery monitoring (re-scanning a stored SBOM against freshly ingested
CVEs) is a separate follow-up - this is the BUILD-TIME gate.
"""
from __future__ import annotations

SBOM_FIX_PREFIX = "SECURITY SCAN: this DevSecOps build ships dependencies with CRITICAL known vulnerabilities"


def blocking(scan: dict | None) -> list[dict]:
    """The CRITICAL CVE findings that BLOCK the build. HIGH is advisory."""
    if not scan or not scan.get("scanned"):
        return []
    return [f for f in (scan.get("findings") or []) if f.get("severity") == "CRITICAL"]


def fix_instruction(scan: dict) -> str:
    lines = []
    for f in blocking(scan)[:15]:
        fixed = f" -> upgrade to {f['fixed']}" if f.get("fixed") else " (no fixed version published)"
        lines.append(f"- {f.get('pkg')}@{f.get('version')}: {f.get('id')} (CRITICAL){fixed}")
    return (
        SBOM_FIX_PREFIX + " (CVEs). This is a hardened DevSecOps deliverable and must not "
        "ship a critical vulnerability. Upgrade ONLY the affected dependencies to a fixed "
        "version (keep everything working; do not rewrite the project) and, where no fix "
        "exists, replace the dependency:\n\n" + "\n".join(lines))
