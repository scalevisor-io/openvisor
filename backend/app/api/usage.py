"""§usage graph: consumption over time, for the project Usage tab.

The ledger is the only per-event record of what a model call cost (project
counters are running totals with no history), so the series is built from
`credit_transaction` rows: one bucket per UTC day, tokens and credits summed,
split by what drove the spend. Read-only and cheap - a bounded window, one
grouped query, no LLM anywhere near it.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_project_for_user
from app.api.mcp_tokens import positive
from app.models import CreditTransaction, Project, utcnow

router = APIRouter(prefix="/api/projects/{project_id}/usage", tags=["usage"])

# What a spend row means to a customer, keyed by ledger kind. `consumption`
# covers builds, chat answers and classifier calls; mcp_query is MCP traffic.
SPEND_KINDS = ("consumption", "mcp_query", "program_run")


@router.get("")
async def project_usage(days: int = Query(30, ge=1, le=365),
                        project: Project = Depends(get_project_for_user),
                        db: AsyncSession = Depends(get_db)):
    """Daily token/credit consumption for this project over the window.

    Returns EVERY day in the range, including zeros - a sparse series would draw
    a chart that lies about the gaps."""
    since = (utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0,
                                                          microsecond=0)
    day = func.date_trunc("day", CreditTransaction.created_at).label("day")
    rows = (await db.execute(
        select(day, CreditTransaction.kind,
               func.coalesce(func.sum(CreditTransaction.tokens), 0).label("tokens"),
               func.coalesce(func.sum(CreditTransaction.amount), 0.0).label("amount"))
        .where(CreditTransaction.project_id == project.id,
               CreditTransaction.created_at >= since,
               CreditTransaction.kind.in_(SPEND_KINDS))
        .group_by(day, CreditTransaction.kind).order_by(day))).all()

    buckets: dict[date, dict] = {}
    start = since.date()
    for i in range(days):
        d = start + timedelta(days=i)
        buckets[d] = {"day": d.isoformat(), "tokens": 0, "credits": 0.0, "mcp_tokens": 0}
    for r in rows:
        d = r.day.date()
        if d not in buckets:  # clock skew / a row on the boundary
            continue
        buckets[d]["tokens"] += int(r.tokens or 0)
        # amounts are negative debits; the chart wants a positive magnitude
        buckets[d]["credits"] = positive(buckets[d]["credits"] + float(-r.amount), 6)
        if r.kind == "mcp_query":
            buckets[d]["mcp_tokens"] += int(r.tokens or 0)

    series = list(buckets.values())
    return {
        "days": days,
        "series": series,
        "totals": {
            "tokens": sum(b["tokens"] for b in series),
            "credits": positive(sum(b["credits"] for b in series)),
            "mcp_tokens": sum(b["mcp_tokens"] for b in series),
            # lifetime, from the project counters - the window is only a slice
            "lifetime_tokens": project.tokens_consumed or 0,
            "lifetime_credits": positive(project.cost_credits or 0.0),
        },
    }
