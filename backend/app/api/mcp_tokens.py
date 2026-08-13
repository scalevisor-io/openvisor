"""§MCP project tokens: the per-project MCP surface behind a project's MCP tab.

A project token is a prefixed API token narrowed to ONE project: the MCP sidecar
shows it only that project's tools, and every query it makes bills the project
like any other work on it (instead of the org-level `mcp_query` row a plain user
token writes). It still carries its minting user - the sidecar's auth query
inner-joins `"user"`, so a user-less token would authenticate to nothing.

PRIVACY: the usage view here counts queries and tokens. It never stores or
returns what was asked - an MCP query comes from someone's terminal agent and
that conversation is theirs (see services/knowledge.answer_question).
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, get_project_for_user
from app.core.security import new_api_token
from app.models import ApiToken, CreditTransaction, Project, User, utcnow
from app.schemas.schemas import ApiTokenIn

router = APIRouter(prefix="/api/projects/{project_id}/mcp-tokens", tags=["mcp"])

MAX_TOKENS_PER_PROJECT = 10


def positive(value: float, digits: int = 4) -> float:
    """Round a magnitude, never returning -0.0.

    Ledger amounts are negative debits, so every display value here is a
    negation - and negating a zero sum yields -0.0, which formats as "-0" in
    the UI. `or 0.0` collapses it (-0.0 is falsy) without touching real values.
    """
    return round(value, digits) or 0.0


def _out(t: ApiToken) -> dict:
    return {"id": t.id, "name": t.name, "created_at": t.created_at,
            "last_used_at": t.last_used_at}


@router.get("")
async def list_project_tokens(project: Project = Depends(get_project_for_user),
                              db: AsyncSession = Depends(get_db)):
    """The project's MCP tokens (never the secrets - only the token prefix ever
    left the mint call) plus the query counters for its MCP tab."""
    rows = (await db.execute(
        select(ApiToken).where(ApiToken.project_id == project.id, ApiToken.scope == "project")
        .order_by(ApiToken.created_at.desc()))).scalars().all()

    since = utcnow() - timedelta(days=30)
    q = select(func.count(), func.coalesce(func.sum(CreditTransaction.amount), 0.0)).where(
        CreditTransaction.project_id == project.id,
        CreditTransaction.kind == "mcp_query")
    total_n, total_credits = (await db.execute(q)).one()
    recent_n, recent_credits = (await db.execute(
        q.where(CreditTransaction.created_at >= since))).one()
    return {
        "tokens": [_out(t) for t in rows],
        "usage": {"queries_total": total_n, "credits_total": positive(-total_credits),
                  "queries_30d": recent_n, "credits_30d": positive(-recent_credits)},
    }


@router.post("", status_code=201)
async def create_project_token(body: ApiTokenIn,
                               project: Project = Depends(get_project_for_user),
                               user: User = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)):
    """Mint a project-scoped token. The plaintext is returned ONCE - only its
    sha256 is stored, exactly like a user token."""
    n = (await db.execute(
        select(func.count()).select_from(ApiToken)
        .where(ApiToken.project_id == project.id, ApiToken.scope == "project"))).scalar_one()
    if n >= MAX_TOKENS_PER_PROJECT:
        raise HTTPException(409, f"This project already has {MAX_TOKENS_PER_PROJECT} MCP tokens - "
                                 "revoke one first")
    plaintext, token_hash = new_api_token()
    row = ApiToken(user_id=user.id, token_hash=token_hash, name=body.name,
                   scope="project", project_id=project.id)
    db.add(row)
    await db.commit()
    return {**_out(row), "token": plaintext}


@router.delete("/{token_id}")
async def revoke_project_token(token_id: str,
                               project: Project = Depends(get_project_for_user),
                               db: AsyncSession = Depends(get_db)):
    row = await db.get(ApiToken, token_id)
    # Scope + project must both match: a token id from another project (or a
    # plain user token) is a 404 here, never a cross-project revoke.
    if row is None or row.project_id != project.id or row.scope != "project":
        raise HTTPException(404, "Not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True}
