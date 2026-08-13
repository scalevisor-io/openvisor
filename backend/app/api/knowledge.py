"""Token-authed knowledge endpoint backing the MCP `search_knowledge` tool.
Bearer API token -> org; returns a synthesized, cited answer and meters the
model cost (x CREDIT_MARKUP) against the org wallet. The sync RAG/LLM work runs
in a threadpool so it never blocks the event loop."""
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


def _run(org_id: str, query: str, k: int, project_id: str | None) -> dict:
    with SyncSession() as db:
        # Re-read the project in THIS session (the async dep's instance belongs
        # to another one); a project-scoped token bills its project.
        project = db.get(Project, project_id) if project_id else None
        result = knowledge.answer_question(db, org_id, query, k, project=project)
        db.commit()
        return result


@router.post("/answer")
async def answer(body: KnowledgeQueryIn, request: Request,
                 ctx: tuple[User, Project | None] = Depends(require_api_token_project)):
    user, project = ctx
    # Per-org cap: the MCP is a single caller IP, so key the limiter on the org.
    await rate_limit(request, "mcp_knowledge", 30, 60, identity=user.org_id)
    try:
        return await run_in_threadpool(_run, user.org_id, body.query, body.k,
                                       project.id if project else None)
    except knowledge.InsufficientCredits:
        raise HTTPException(402, f"Insufficient credits. Top up at {settings.app_base_url}/billing")
    except knowledge.KnowledgeConfigError as exc:
        log.error("knowledge misconfigured: %s", exc)
        raise HTTPException(503, "Knowledge service is temporarily unavailable")
    except LLMUnavailable as exc:
        log.warning("knowledge LLM unavailable: %s", exc)
        raise HTTPException(503, "Knowledge service is temporarily unavailable")
