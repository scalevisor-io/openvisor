"""Harness-version fingerprint - the one discipline that makes attribution possible.

Stamp a stable hash of the HARNESS CONFIG on every run, and never compare two runs
across different versions. Everything that changes agent behaviour EXCEPT the model is
folded in: the system/review prompts (by content), the tool preset, the caps, the RAG
retrieval params, and the boot-check flag. The MODEL id is recorded SEPARATELY (it is
not part of the harness) so a model swap can never masquerade as a harness change - and
vice versa.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

# backend/app/services/agent_eval/ -> backend/app/agents/prompts/
PROMPT_DIR = Path(__file__).resolve().parents[2] / "agents" / "prompts"

# The tool set the runner constructs (runner/run_dev.py: get_default_agent). This is a
# STRING on purpose: bump it the moment the runner's tool preset changes (e.g. adding
# grep/glob, or a validate-before-apply editor) so the version moves with the harness.
# §dev harness: this is the OpenHands driver's id and the default; a run stamps the
# preset id of the harness it actually resolved to (services/dev_harness.Harness.
# tool_preset_id), which is what keeps two harnesses from sharing a fingerprint.
TOOL_PRESET_ID = "openhands-default:terminal+file_editor+task_tracker+grep+glob"

# RAG breadth is hardcoded in workers/tasks.py::_build_task_file (k=6). Kept here so a
# change to it shifts the fingerprint even though it lives elsewhere.
RAG_K = 6


def _file_hash(p: Path) -> str:
    try:
        return hashlib.blake2b(p.read_bytes(), digest_size=8).hexdigest()
    except OSError:
        return "absent"


def harness_config(settings, prompt_dir: Path | None = None,
                   tool_preset_id: str = TOOL_PRESET_ID) -> dict:
    """The canonical, hashable description of the current harness (model excluded).
    Returned as a plain dict so it can be logged next to a run for transparency."""
    pd = prompt_dir or PROMPT_DIR
    return {
        "schema": 1,
        "tool_preset": tool_preset_id,
        "prompts": {
            "development_system.md": _file_hash(pd / "development_system.md"),
            "security_review.md": _file_hash(pd / "security_review.md"),
        },
        "caps": {
            "dev_max_iterations": settings.dev_max_iterations_default,
            "dev_run_timeout_minutes": settings.dev_run_timeout_minutes,
            "dev_boot_fix_attempts": settings.dev_boot_fix_attempts,
            "ci_max_retries": settings.ci_max_retries,
            "security_fix_attempts": settings.security_fix_attempts,
        },
        "retrieval": {"k": RAG_K, "kb_retrieval_min_score": settings.kb_retrieval_min_score,
                      "speciality_tag_filter": True,
                      "procedures_k": settings.kb_procedures_k},
        # contract_lint: the deterministic pre-boot demo-contract check (services/contract.py)
        # runs as part of the boot gate. Presence here moves the fingerprint so pre- and
        # post-linter runs are never compared.
        "gates": {"boot_check": settings.dev_boot_check, "contract_lint": True,
                  "acceptance_checks": settings.acceptance_checks_enabled,
                  "sovereign_gate": True, "devsecops_sbom_gate": True},
        # kb_rules_digest: §KB tiers standing-rules injection; the value is the digest
        # budget (chars) so a budget change also shifts the fingerprint. The digest
        # CONTENT is corpus data (like KB content, not harness) and is not hashed.
        "overlays": {"deliverable_clause": True, "one_shot_scaffold": True,
                     "kb_rules_digest": settings.kb_rules_digest_max_chars},
    }


def compute_harness_version(settings=None, prompt_dir: Path | None = None,
                            tool_preset_id: str = TOOL_PRESET_ID) -> str:
    """A short, stable id like 'hv_1a2b3c4d5e6f'. Same config -> same id (order-
    independent); any covered change -> a different id."""
    if settings is None:
        from app.core.config import settings as _s
        settings = _s
    cfg = harness_config(settings, prompt_dir, tool_preset_id)
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
    return "hv_" + hashlib.blake2b(blob, digest_size=6).hexdigest()
