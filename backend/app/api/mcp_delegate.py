"""§MCP delegate: hand a slice of development to the platform over MCP.

The caller's own agent is building something and wants a part of it done here:
`delegate` files a Request on the token's project and starts it, and the §14
pipeline takes over unchanged - scoped dev run, branch, PR on the project's push
repo, security review, boot gate, billing. `get_delegation` is how the caller's
agent follows it, because a dev run takes minutes and no MCP client should block
on that.

Nothing new is invented here: these wrap the SAME `services/project_actions`
functions the SPA and the hub call, so the guards can't drift between surfaces.
The one thing this layer adds is a per-project daily cap - an MCP token is a
credential in someone's terminal, and delegation is the only tool that can spend
build-sized money.

PRIVACY NOTE, deliberately different from `consult`: a delegation spec IS
stored. It is the work order - the agent builds from it, the PR description
cites it, and it is what justifies the charge. Only consult questions are
ephemeral.
"""
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request as HttpRequest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import rate_limit, require_api_token_project
from app.models import Organization, Project, Request, User, utcnow
from app.schemas.schemas import McpConsultIn, McpDelegateIn
from app.services import events, project_actions, repos as repolib
from app.workers.celery_app import celery

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _delegation_out(req: Request, project: Project) -> dict:
    """What the caller's agent needs to follow the work - never the thread."""
    prs = [p.get("url") for p in (req.pr_urls or []) if p.get("url")]
    return {
        "request_id": req.id,
        "title": req.title,
        "type": req.type,
        "status": req.status,
        "created_at": req.created_at,
        "pull_requests": prs,
        "tokens_consumed": req.tokens_consumed or 0,
        "credits": round(req.cost_credits or 0.0, 4),
        "url": f"{settings.app_base_url}/projects/{project.id}/requests/{req.id}",
        "build_state": project.dev_run_state if project.dev_request_id == req.id else None,
    }


async def _token_project(ctx: tuple[User, Project | None]) -> tuple[User, Project]:
    user, project = ctx
    if project is None:
        raise HTTPException(403, "Delegation needs a project-scoped MCP token - mint one "
                                 "from the project's MCP tab")
    return user, project


@router.post("/delegate", status_code=201)
async def delegate(body: McpDelegateIn, request: HttpRequest,
                   ctx: tuple[User, Project | None] = Depends(require_api_token_project),
                   db: AsyncSession = Depends(get_db)):
    """File and start a delegated piece of work. Returns immediately with the
    request id - the build runs for minutes, so the caller polls
    `GET /mcp/delegations/{id}` (or watches the PR appear on their repo)."""
    user, project = await _token_project(ctx)
    await rate_limit(request, "mcp_delegate", 20, 600, identity=user.org_id)

    # A delegation's deliverable is a pull request, so there has to be somewhere
    # to push. Refusing here beats a run that dies at publish time.
    await db.refresh(project, ["repos"])
    push = next((r for r in project.repos if r.is_push_target), None)
    if push is None and not project.gitlab_project_id:
        raise HTTPException(409, "This project has no repository to open a pull request on - "
                                 "connect one first")
    if push is not None and push.provider not in repolib.AUTO_MERGE_PROVIDERS:
        log.info("delegation on a non-PR host for %s (%s)", project.id, push.provider)

    org = await db.get(Organization, project.org_id)
    if (org.credit_balance or 0.0) <= 0:
        raise HTTPException(402, f"Insufficient credits. Top up at {settings.app_base_url}/billing")

    # Daily cap: this is the one MCP tool that spends build-sized money, and the
    # token lives in someone's terminal.
    since = utcnow() - timedelta(days=1)
    started = (await db.execute(
        select(func.count()).select_from(Request)
        .where(Request.project_id == project.id, Request.created_at >= since,
               Request.handling == "ai"))).scalar_one()
    if started >= settings.mcp_delegate_daily_max:
        raise HTTPException(429, f"Daily delegation limit reached for this project "
                                 f"({settings.mcp_delegate_daily_max}/day)")

    try:
        req, _ = await project_actions.create_request(
            db, project, "customer", body.type, "ai", body.spec)
        req = await project_actions.start_request(db, project, req.id)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return _delegation_out(req, project)


@router.get("/delegations")
async def list_delegations(request: HttpRequest, limit: int = 20,
                           ctx: tuple[User, Project | None] = Depends(require_api_token_project),
                           db: AsyncSession = Depends(get_db)):
    user, project = await _token_project(ctx)
    await rate_limit(request, "mcp_read", 120, 600, identity=user.org_id)
    rows = (await db.execute(
        select(Request).where(Request.project_id == project.id, Request.type != "mvp")
        .order_by(Request.created_at.desc()).limit(min(max(limit, 1), 50)))).scalars().all()
    return {"delegations": [_delegation_out(r, project) for r in rows]}


@router.get("/delegations/{request_id}")
async def get_delegation(request_id: str, request: HttpRequest,
                         ctx: tuple[User, Project | None] = Depends(require_api_token_project),
                         db: AsyncSession = Depends(get_db)):
    user, project = await _token_project(ctx)
    await rate_limit(request, "mcp_read", 120, 600, identity=user.org_id)
    req = await db.get(Request, request_id)
    if req is None or req.project_id != project.id:
        raise HTTPException(404, "Unknown delegation")
    return _delegation_out(req, project)


# ---------------------------------------------------------------- consult (1b)

@router.post("/consult", status_code=202)
async def consult(body: McpConsultIn, request: HttpRequest,
                  ctx: tuple[User, Project | None] = Depends(require_api_token_project),
                  db: AsyncSession = Depends(get_db)):
    """§MCP consult (mode 1b): ask a question ABOUT this project's codebase and
    get it answered by the dev harness reading the actual repositories - no
    edits, no commit, no push.

    202 with a job id, because that run takes minutes; the caller polls
    `GET /mcp/consult/{job_id}`. Cheap KB questions should use `search_knowledge`
    instead - this one is priced like a build.

    The QUESTION is never persisted: it rides to the worker and into the sandbox
    task, and the answer waits in redis under a TTL for its caller. Nothing about
    it reaches the database."""
    from uuid import uuid4

    user, project = await _token_project(ctx)
    await rate_limit(request, "mcp_consult", 10, 600, identity=user.org_id)

    org = await db.get(Organization, project.org_id)
    if (org.credit_balance or 0.0) <= 0:
        raise HTTPException(402, f"Insufficient credits. Top up at {settings.app_base_url}/billing")

    job_id = str(uuid4())
    events.get_sync_redis().set(f"mcpconsult:{job_id}",
                                '{"state": "queued"}', ex=3600)
    celery.send_task("app.workers.tasks.run_mcp_consult",
                     args=[project.id, job_id, body.question])
    return {"job_id": job_id, "state": "queued",
            "note": "Reading the repository takes a few minutes - poll get_consult."}


@router.get("/consult/{job_id}")
async def get_consult(job_id: str, request: HttpRequest,
                      ctx: tuple[User, Project | None] = Depends(require_api_token_project)):
    """Poll a consult. `queued`/`running` mean keep waiting; `done` carries the
    answer (once - it expires with the redis key), `failed` carries why."""
    import json

    user, _ = await _token_project(ctx)
    await rate_limit(request, "mcp_read", 120, 600, identity=user.org_id)
    raw = events.get_sync_redis().get(f"mcpconsult:{job_id}")
    if raw is None:
        raise HTTPException(404, "Unknown or expired consult")
    data = json.loads(raw if isinstance(raw, str) else raw.decode())
    return {"job_id": job_id, **data}
