"""Spec-derived acceptance checks (§Phase 1 #5).

Generate a small set of deterministic HTTP checks from the project spec and run
them against the BOOTED demo in the boot-gate sandbox: the boot gate proves the
app STARTS, these prove it CONFORMS to what was asked. LLM-written checks are
unreliable, so this is a best-effort FILTER, never a hard gate - results are
advisory (recorded + surfaced), a failing check never blocks delivery, and any
generation/validation problem falls back to no checks (boot-only).

Checks are validated STRICTLY here before they reach the deployer, which fetches
each path with busybox wget inside the demo DinD and matches `contains` in Python
(never a shell) - so an LLM-authored path or fragment can never inject a command
into the sandbox.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.agents.pipeline import LLMUnavailable, _project_context, chat_json, load_prompt
from app.models import Project, Request
from app.services.llm import record_usage

log = logging.getLogger(__name__)

MAX_CHECKS = 5
# A safe HTTP path: plain path segments only. Query strings and every shell
# metacharacter are rejected - a conformance check needs only simple GET paths, and
# staying strict keeps LLM-authored paths trivially injection-proof (belt-and-braces
# on top of the deployer passing the URL as a quoted argv element, never via a shell).
_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.\-~]*$")
# A `contains` fragment must be plain text - reject control chars and shell metachars.
_FRAG_BAD = re.compile(r"""[\x00-\x1f`$;|&<>"'\\]""")


def _valid_check(c) -> dict | None:
    """Coerce + STRICTLY validate one model-authored check; None drops it. The
    path must be a safe URL path and every `contains` fragment plain text, so the
    deployer can never be shell-injected by a generated check."""
    if not isinstance(c, dict):
        return None
    path = str(c.get("path") or "").strip()
    if len(path) > 120 or not _PATH_RE.match(path):
        return None
    raw = c.get("contains")
    if not isinstance(raw, list):
        return None
    frags: list[str] = []
    for f in raw:
        s = str(f or "").strip()
        if s and len(s) <= 60 and not _FRAG_BAD.search(s):
            frags.append(s)
    if not frags:
        return None
    return {"path": path, "contains": frags[:3], "desc": str(c.get("desc") or "")[:120]}


def generate_checks(db: Session, project: Project) -> list[dict]:
    """Spec -> a small list of validated {path, contains, desc} checks. Returns []
    on ANY problem (fallback: boot-only). Metered against the project. Best-effort:
    never raises into the boot gate."""
    try:
        spec = _project_context(db, project)
        result, usage = chat_json([
            {"role": "system", "content": load_prompt("acceptance_checks.md")},
            {"role": "user", "content": "Project spec:\n<spec>\n" + spec[:8000] + "\n</spec>"},
        ], max_tokens=800)
        req = db.get(Request, project.dev_request_id) if project.dev_request_id else None
        record_usage(db, project, usage, "acceptance checks", request=req)
    except LLMUnavailable:
        return []
    except Exception as exc:  # noqa: BLE001 - generation must never fail a build
        log.warning("acceptance-check generation skipped for %s: %s", project.id, exc)
        return []
    raw = result.get("checks") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return []
    checks = [v for v in (_valid_check(c) for c in raw[: MAX_CHECKS * 2]) if v]
    return checks[:MAX_CHECKS]
