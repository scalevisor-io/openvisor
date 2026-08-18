"""§MCP projects: create the project an MCP client works through.

Two doors onto one service (services/mcp_projects.create), because two very
different callers need it:

- `POST /api/projects/mcp` - the SPA's one-click flow on /settings/tokens.
  Session-authed, and it mints the project's first token in the same call so the
  customer leaves with a working `claude mcp add` line.
- `POST /api/mcp/projects` - the MCP `create_project` tool, reached with an
  ACCOUNT-WIDE bearer token (a project token is bound to one project and can
  never create a sibling). It deliberately returns NO token: an agent that could
  mint its own long-lived credential would write it into a transcript, so the
  customer mints it on the tokens page instead.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import project_out
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import rate_limit, require_api_token_project, require_verified
from app.core.security import new_api_token
from app.models import ApiToken, Project, User
from app.schemas.schemas import McpProjectCreateIn
from app.services import mcp_projects

log = logging.getLogger(__name__)
# Split routers: the SPA door rides session auth + CSRF, the tool door is
# bearer-token only (the sidecar sends no cookies), exactly like knowledge.
router = APIRouter(tags=["mcp"])
tool_router = APIRouter(tags=["mcp"])

# One org can't turn the one-click button into an unbounded project factory.
MAX_MCP_PROJECTS_PER_ORG = 100


async def _guard_quota(db: AsyncSession, user: User) -> None:
    n = (await db.execute(
        select(func.count()).select_from(Project)
        .where(Project.org_id == user.org_id, Project.kind == mcp_projects.KIND))).scalar_one()
    if n >= MAX_MCP_PROJECTS_PER_ORG:
        raise HTTPException(
            409, f"This organization already has {MAX_MCP_PROJECTS_PER_ORG} MCP projects - "
                 "reuse or delete one first")


@router.get("/api/mcp/tokens")
async def list_my_project_tokens(user: User = Depends(require_verified),
                                 db: AsyncSession = Depends(get_db)):
    """Every project-scoped MCP token in the caller's org, with the project it is
    bound to - what /settings/tokens groups its MCP tab by. Secrets never appear
    here; only the mint call ever returned one."""
    rows = (await db.execute(
        select(ApiToken, Project.name, Project.kind)
        .join(Project, Project.id == ApiToken.project_id)
        .where(ApiToken.scope == "project", Project.org_id == user.org_id)
        .order_by(Project.name, ApiToken.created_at.desc()))).all()
    return [{"id": t.id, "name": t.name, "created_at": t.created_at,
             "last_used_at": t.last_used_at, "project_id": t.project_id,
             "project_name": pname, "project_kind": pkind}
            for t, pname, pkind in rows]


@router.post("/api/projects/mcp", status_code=201)
async def create_mcp_project(body: McpProjectCreateIn,
                             user: User = Depends(require_verified),
                             db: AsyncSession = Depends(get_db)):
    """One click on /settings/tokens: an MCP project plus its first token. The
    plaintext token is returned ONCE, exactly like every other mint."""
    await _guard_quota(db, user)
    project = await mcp_projects.create(db, user, body.title, body.description)
    plaintext, token_hash = new_api_token()
    row = ApiToken(user_id=user.id, token_hash=token_hash,
                   name=(body.token_name or "MCP client").strip(),
                   scope="project", project_id=project.id)
    db.add(row)
    await db.commit()
    await db.refresh(project, ["repos"])
    return {"project": project_out(project), "token": plaintext, "token_name": row.name,
            "mcp_url": f"{settings.http_scheme}://mcp.{settings.deploy_domain}"
                       f"{settings.public_port_suffix}/mcp"}


@tool_router.post("/api/mcp/projects", status_code=201)
async def create_mcp_project_from_tool(
        body: McpProjectCreateIn, request: Request,
        ctx: tuple[User, Project | None] = Depends(require_api_token_project),
        db: AsyncSession = Depends(get_db)):
    """The MCP `create_project` tool. Account-wide tokens only: a project-scoped
    token is that project and has no business making others."""
    user, token_project = ctx
    if token_project is not None:
        raise HTTPException(
            403, "create_project needs an account-wide MCP token; this token is bound to a "
                 "single project.")
    await rate_limit(request, "mcp_create_project", 10, 60, identity=user.org_id)
    await _guard_quota(db, user)
    project = await mcp_projects.create(db, user, body.title, body.description)
    await db.commit()
    return {"project_id": project.id, "name": project.name, "status": project.status,
            "next_step": (
                f"Mint this project's MCP token at {settings.app_base_url}/projects/"
                f"{project.id} (MCP tab), then connect it as its own MCP server - that "
                "token is what unlocks search_knowledge, consult_codebase and "
                "delegate_development for this project.")}
