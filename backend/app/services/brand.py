"""White-label brand helpers. All user-facing brand strings flow through here:
email subjects, prompt files and static_data are rendered with the placeholders
below so a spoke deployment rebrands via env vars only (BRAND_NAME,
CONSULTANT_NAME - see core/config.py for the defaults).

§consultant identity: the consultant's name is the exception - it is also
admin-editable (Settings -> Consultant, stored as two fields in `app_setting`),
because "who is the consultant" is the one brand string that changes without a
redeploy. `consultant_name()` and `consultant_first_name()` are the ONLY answer
to that question: they read the stored pair, fall back to CONSULTANT_NAME, and
are what every prompt, email and API payload goes through. Reading
`settings.consultant_name` directly gives the env value and silently ignores
the admin - don't.
"""
import logging
import time

from app.core.config import settings

log = logging.getLogger(__name__)

# The stored pair is read once per process and cached: it is wanted on paths
# with no session (prompt rendering, the Celery emailer) and it changes about
# never, so the alternative is a database round trip per rendered string. The
# TTL is what makes an admin edit land everywhere without a restart; the API
# process that took the edit also drops the cache outright (`reset_cache`).
_TTL_SECONDS = 300
_cache: tuple[float, dict[str, str]] | None = None


def _stored() -> dict[str, str]:
    """The admin's first/last name pair, empty strings when unset or unreadable.
    Never raises: a brand string is not worth failing a request over."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _TTL_SECONDS:
        return _cache[1]
    out = {"consultant_first_name": "", "consultant_last_name": ""}
    try:
        from app.core.db import SyncSession
        from app.services.app_settings import get_consultant_identity_sync
        with SyncSession() as db:
            out = get_consultant_identity_sync(db)
    except Exception as exc:  # noqa: BLE001 - fall back to the env identity
        log.warning("consultant identity unavailable, using CONSULTANT_NAME: %s", exc)
    _cache = (now, out)
    return out


def reset_cache() -> None:
    """Drop the cached §consultant identity (the admin just saved it, or a test)."""
    global _cache
    _cache = None


def _env_parts() -> tuple[str, str]:
    """CONSULTANT_NAME split into first and rest - the fallback identity, and
    the only place that guesses where a name divides."""
    parts = settings.consultant_name.split()
    if not parts:
        return ("Consultant", "")
    return (parts[0], " ".join(parts[1:]))


def consultant_parts() -> tuple[str, str]:
    """`(first, last)`. Each field falls back to the env identity on its own, so
    an admin who fills in only the first name still gets the env surname."""
    stored = _stored()
    env_first, env_last = _env_parts()
    return (stored["consultant_first_name"] or env_first,
            stored["consultant_last_name"] or env_last)


def consultant_first_name() -> str:
    return consultant_parts()[0]


def consultant_name() -> str:
    """The full name, single-spaced (a consultant with no surname is just the
    first name - never a trailing space)."""
    return " ".join(p for p in consultant_parts() if p)


_PLACEHOLDERS = {
    "{{BRAND_NAME}}": lambda: settings.brand_name,
    "{{CONSULTANT_NAME}}": consultant_name,
    "{{CONSULTANT_FIRST_NAME}}": consultant_first_name,
    "{{CONSULTANT_FOCUS}}": lambda: settings.consultant_focus,
    # Domain references (demo subdomains) are NOT the brand name: a brand can be
    # "Acme AI" while demos live on *.acme.example.
    "{{DEPLOY_DOMAIN}}": lambda: settings.deploy_domain,
}


def subject(text: str) -> str:
    """Email subject with the brand prefix: "[<brand>] <text>"."""
    return f"[{settings.brand_name}] {text}"


def render(text: str) -> str:
    """Substitute brand placeholders in a prompt/template string."""
    for key, value in _PLACEHOLDERS.items():
        if key in text:
            text = text.replace(key, value())
    return text


def render_obj(obj):
    """Recursively substitute brand placeholders in parsed JSON/YAML data."""
    if isinstance(obj, str):
        return render(obj)
    if isinstance(obj, list):
        return [render_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {k: render_obj(v) for k, v in obj.items()}
    return obj
