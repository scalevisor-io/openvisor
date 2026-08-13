"""§audit: trace WHO did WHAT without logging content. One INFO line on the
dedicated `audit` logger per authenticated MUTATING request: a hashed actor, the
HTTP method, and the matched route TEMPLATE (e.g. `/api/projects/{project_id}`)
- never query strings, request bodies, prompts, chat text, or any payload
detail. The actor hash is a truncated sha256 of the lowercased identifier
(email), stable across requests for correlation but never the identity itself.
Reads are not actions and stay unlogged, so the log volume tracks what users DO.
"""
import hashlib
import logging

from fastapi import Request

log = logging.getLogger("audit")

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def actor_hash(identifier: str | None) -> str:
    """Stable pseudonymous actor id: sha256 of the lowercased identifier,
    truncated - enough to follow one actor through the log, never reversible
    from the log alone."""
    if not identifier:
        return "anonymous"
    return hashlib.sha256(identifier.strip().lower().encode()).hexdigest()[:16]


def log_action(request: Request, identifier: str | None, kind: str = "user") -> None:
    """Record one user action. Called from the auth dependencies (the single
    chokepoint every authenticated route passes), so coverage is every SPA,
    admin, token, and hub mutation without per-route wiring. The route template
    is logged, not the raw URL - path params like project ids stay out."""
    if request.method not in MUTATING:
        return
    route = getattr(request.scope.get("route"), "path", None) or request.url.path
    log.info("actor=%s kind=%s %s %s", actor_hash(identifier), kind,
             request.method, route)
