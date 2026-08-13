"""§28 inbound trigger hooks: verify, normalize and filter a GitHub/GitLab
webhook delivery for a Program instance. Pure functions - the HTTP router
(api/program_hooks.py) does transport, redis dedup/rate caps and enqueueing.

Scope: issue events only for now (`issues` on GitHub, `Issue Hook` on GitLab).
Anything else normalizes to None and is acknowledged without a run.
"""
import hashlib
import hmac
import logging

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000  # webhook payloads are KBs; anything huge is abuse
_BODY_SNIPPET = 20_000  # issue body chars kept in event.json


def verify_signature(secret: str, headers: dict, body: bytes) -> str | None:
    """Return the sending provider ('github'|'gitlab') when the delivery is
    authentic, else None. GitHub signs the raw body (X-Hub-Signature-256,
    'sha256=<hmac>'); GitLab sends the shared secret verbatim (X-Gitlab-Token).
    Constant-time comparisons throughout."""
    gh_sig = headers.get("x-hub-signature-256")
    if gh_sig:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return "github" if hmac.compare_digest(expected, gh_sig) else None
    gl_tok = headers.get("x-gitlab-token")
    if gl_tok:
        return "gitlab" if hmac.compare_digest(secret, gl_tok) else None
    return None


def delivery_id(headers: dict, body: bytes) -> str:
    """The provider's delivery id for replay dedup; body hash as a last resort
    (a provider retry resends the same body, so the hash still dedups)."""
    return (headers.get("x-github-delivery") or headers.get("x-gitlab-event-uuid")
            or hashlib.sha256(body).hexdigest())


def normalize_event(provider: str, headers: dict, payload: dict) -> dict | None:
    """Provider payload -> the engine-agnostic event staged as input/event.json:
    {provider, event: 'issues', action, delivery, issue: {iid, url, title, body,
    labels, assignees, author}}. Non-issue events -> None (acknowledged, no run).
    GitLab's issue hook carries no issue-author username, so `author` there is
    the event ACTOR (who triggered the hook) - document filters accordingly."""
    try:
        if provider == "github":
            if headers.get("x-github-event") != "issues":
                return None
            issue = payload.get("issue") or {}
            if "pull_request" in issue:
                return None  # PR events ride the issues API shape too
            return {
                "provider": "github",
                "event": "issues",
                "action": payload.get("action") or "",
                "issue": {
                    "iid": issue.get("number"),
                    "url": issue.get("html_url", ""),
                    "title": issue.get("title", ""),
                    "body": (issue.get("body") or "")[:_BODY_SNIPPET],
                    "labels": [l.get("name", "") for l in issue.get("labels", []) or []
                               if isinstance(l, dict)],
                    "assignees": [a.get("login", "") for a in issue.get("assignees", []) or []],
                    "author": (issue.get("user") or {}).get("login", ""),
                },
            }
        if provider == "gitlab":
            if payload.get("object_kind") != "issue":
                return None
            oa = payload.get("object_attributes") or {}
            return {
                "provider": "gitlab",
                "event": "issues",
                "action": oa.get("action") or "",
                "issue": {
                    "iid": oa.get("iid"),
                    "url": oa.get("url", ""),
                    "title": oa.get("title", ""),
                    "body": (oa.get("description") or "")[:_BODY_SNIPPET],
                    "labels": [l.get("title", "") for l in payload.get("labels", []) or []
                               if isinstance(l, dict)],
                    "assignees": [a.get("username", "") for a in payload.get("assignees", []) or []],
                    "author": (payload.get("user") or {}).get("username", ""),
                },
            }
    except Exception as exc:  # noqa: BLE001 - malformed payloads never 500 the receiver
        log.warning("program hook payload not understood (%s): %s", provider, exc)
    return None


def event_matches(filters: dict, event: dict) -> bool:
    """AND-composed allowlists; an EMPTY list is 'no constraint' (the signed
    secret is the auth - filters only narrow). actions match the provider's
    native action string; labels/assignees any-of; authors matches the issue
    author (GitHub) / event actor (GitLab)."""
    issue = event.get("issue") or {}
    actions = filters.get("actions") or []
    if actions and event.get("action") not in actions:
        return False
    labels = filters.get("labels") or []
    if labels and not set(labels) & set(issue.get("labels") or []):
        return False
    assignees = filters.get("assignees") or []
    if assignees and not set(assignees) & set(issue.get("assignees") or []):
        return False
    authors = filters.get("authors") or []
    if authors and issue.get("author") not in authors:
        return False
    return True
