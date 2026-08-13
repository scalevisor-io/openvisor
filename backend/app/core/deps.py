"""FastAPI dependencies: session auth, CSRF, admin/verified guards, rate limit."""
import logging

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_db
from app.core.security import CSRF_COOKIE, SESSION_COOKIE, hash_api_token, read_session_token
from app.models import ApiToken, Project, ProjectShare, User, utcnow
from app.services.events import get_async_redis

log = logging.getLogger(__name__)

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


async def csrf_protect(request: Request) -> None:
    if request.method not in MUTATING:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or cookie != header:
        raise HTTPException(403, "CSRF token missing or invalid")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    uid = read_session_token(token) if token else None
    user = await db.get(User, uid) if uid else None
    if user is None:
        raise HTTPException(401, "Not authenticated")
    # §audit: one line per authenticated mutation (hashed actor + route template,
    # never content). Resolved once per request - FastAPI caches the dependency.
    audit.log_action(request, user.email)
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


async def require_verified(user: User = Depends(get_current_user)) -> User:
    if not user.email_verified and user.role != "admin":
        raise HTTPException(403, "email_not_verified")
    return user


async def _resolve_api_token(request: Request, db: AsyncSession) -> tuple[ApiToken, User]:
    """Authenticate a prefixed API token (Authorization: Bearer) and resolve it to
    (token row, owning user). Bumps last_used_at. 401 on missing/unknown token.
    Mirrors the MCP server's sha256 lookup, in SQLAlchemy. Scope enforcement is
    left to the caller (require_api_token / require_hub_token)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token_hash = hash_api_token(auth[7:].strip())
    row = (await db.execute(
        select(ApiToken).where(ApiToken.token_hash == token_hash))).scalar_one_or_none()
    if row is None:
        raise HTTPException(401, "Invalid API token")
    row.last_used_at = utcnow()
    user = await db.get(User, row.user_id)
    await db.commit()
    return row, user


# Token scopes that authenticate a CUSTOMER to the knowledge/MCP surface. A
# "project" token (§MCP project tokens) is a user token narrowed to one project:
# same customer surface, but its queries bill that project.
CUSTOMER_TOKEN_SCOPES = ("user", "project")


async def require_api_token(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """A customer-scoped API token → its owning user (→ org). Hub tokens are
    rejected with 403: they must never bill knowledge queries to a wallet."""
    row, user = await _resolve_api_token(request, db)
    if row.scope not in CUSTOMER_TOKEN_SCOPES:
        raise HTTPException(403, "This token cannot access user endpoints")
    audit.log_action(request, user.email if user else None, kind="token")
    return user


async def require_api_token_project(
        request: Request, db: AsyncSession = Depends(get_db)) -> tuple[User, Project | None]:
    """require_api_token, plus the project a `project`-scoped token is bound to
    (None for a plain user token). The project must still belong to the token
    owner's org - a project moved or deleted under the token authenticates to
    nothing rather than to someone else's work."""
    row, user = await _resolve_api_token(request, db)
    if row.scope not in CUSTOMER_TOKEN_SCOPES:
        raise HTTPException(403, "This token cannot access user endpoints")
    audit.log_action(request, user.email if user else None, kind="token")
    project = None
    if row.project_id:
        project = await db.get(Project, row.project_id)
        if project is None or project.org_id != user.org_id:
            raise HTTPException(403, "This token's project is no longer available")
    return user, project


async def require_hub_token(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """A hub-scoped API token → its owning (admin) user. Guards the /api/hub
    control surface; user tokens are rejected with 403."""
    row, user = await _resolve_api_token(request, db)
    if row.scope != "hub":
        raise HTTPException(403, "Hub token required")
    audit.log_action(request, user.email if user else None, kind="hub")
    return user


async def project_access_role(db: AsyncSession, user: User, project: Project) -> str | None:
    """The caller's access to a project: 'owner' (admin or the owning org),
    'contributor'/'viewer' (a ProjectShare row, §sharing), or None."""
    if user.role == "admin" or project.org_id == user.org_id:
        return "owner"
    share = (await db.execute(select(ProjectShare).where(
        ProjectShare.project_id == project.id,
        ProjectShare.user_id == user.id))).scalar_one_or_none()
    return share.role if share else None


async def get_project_for_user(
    project_id: str, request: Request,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    access = await project_access_role(db, user, project)
    if access is None:
        raise HTTPException(404, "Project not found")
    # §sharing: a read-only share sees everything and touches nothing - one gate
    # on the HTTP method covers every project route without per-route guards.
    if access == "viewer" and request.method in MUTATING:
        raise HTTPException(403, "This project is shared with you read-only")
    project.access_role = access  # picked up by project_summary/project_out
    return project


async def get_project_for_org_member(
    project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Project:
    """Owner-org (or admin) access only - guards share management (§sharing): a
    shared contributor can never manage who else sees the project."""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if user.role != "admin" and project.org_id != user.org_id:
        raise HTTPException(403, "Only the project's own team can manage sharing")
    project.access_role = "owner"
    return project


async def rate_limit(request: Request, key: str, limit: int, window_s: int,
                     identity: str | None = None) -> None:
    """Fixed-window limiter in Redis. Fails OPEN on a Redis outage (logs a warning
    and allows the request): Redis being unreachable already means the platform is
    degraded, and a 500 from the limiter would only mask the real fault. Also
    self-heals a counter that lost its TTL - a crash between INCR and EXPIRE would
    otherwise leave a TTL-less key stuck at a permanent 429 once over the limit."""
    r = get_async_redis()
    who = identity or (request.client.host if request.client else "unknown")
    redis_key = f"rl:{key}:{who}"
    try:
        count = await r.incr(redis_key)
        # Set the window on the first hit, and re-set it if a prior crash left the
        # counter without a TTL (ttl == -1: key exists but never expires).
        if count == 1 or await r.ttl(redis_key) == -1:
            await r.expire(redis_key, window_s)
    except Exception as exc:  # Redis unreachable - fail open rather than 500
        log.warning("rate_limit unavailable (%s); allowing request: %s", redis_key, exc)
        return
    if count > limit:
        raise HTTPException(429, "Too many requests, slow down")
