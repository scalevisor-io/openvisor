"""Token-authed knowledge endpoint backing the MCP `search_knowledge` tool.

The token must be PROJECT-scoped: the project decides which model answers and
which knowledge bases it may read, and the query bills to it. An account-wide
token is refused here (403) - it has no project, so it has no model, no KB
selection and nothing to attribute the spend to. The sync RAG/LLM work runs in
a threadpool so it never blocks the event loop."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.db import SyncSession
from app.core.deps import rate_limit, require_api_token_project
from app.models import Project, User
from app.schemas.schemas import KnowledgeQueryIn
from app.services import knowledge
from app.services.llm import LLMUnavailable

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _run(org_id: str, query: str, k: int, project_id: str) -> dict:
    with SyncSession() as db:
        # Re-read the project in THIS session (the async dep's instance belongs
        # to another one); the query bills to it and runs on its model.
        project = db.get(Project, project_id)
        result = knowledge.answer_question(db, org_id, query, k, project=project)
        db.commit()
        return result


@router.post("/answer")
async def answer(body: KnowledgeQueryIn, request: Request,
                 ctx: tuple[User, Project | None] = Depends(require_api_token_project)):
    user, project = ctx
    if project is None:
        raise HTTPException(
            403, "search_knowledge needs a project-scoped MCP token: create or open a "
                 "project and mint its token, so the answer uses that project's model "
                 "and knowledge bases and bills to it.")
    # Per-org cap: the MCP is a single caller IP, so key the limiter on the org.
    await rate_limit(request, "mcp_knowledge", 30, 60, identity=user.org_id)
    try:
        return await run_in_threadpool(_run, user.org_id, body.query, body.k, project.id)
    except knowledge.InsufficientCredits:
        raise HTTPException(402, f"Insufficient credits. Top up at {settings.app_base_url}/billing")
    except knowledge.KnowledgeConfigError as exc:
        log.error("knowledge misconfigured: %s", exc)
        raise HTTPException(503, "Knowledge service is temporarily unavailable")
    except LLMUnavailable as exc:
        log.warning("knowledge LLM unavailable: %s", exc)
        raise HTTPException(503, "Knowledge service is temporarily unavailable")
