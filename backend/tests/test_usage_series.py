"""§usage graph: the daily consumption series behind the project Usage tab.

The ledger is the only per-event record of tokens (project.tokens_consumed is a
running total with no history), so these pin that every metering path stamps
`CreditTransaction.tokens` and that the series is dense - one bucket per day in
the window, zeros included, or the chart draws a lie about the quiet days.
"""
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.models import CreditTransaction, Organization, Project, Request, utcnow


@pytest.fixture
def org_project():
    with SyncSession() as db:
        org = Organization(name="Usage Series Org", credit_balance=100.0)
        db.add(org)
        db.commit()
        p = Project(org_id=org.id, name="U", description="d", kind="ai",
                    status="development", workspace_path="/tmp/usage")
        db.add(p)
        db.commit()
        ids = (org.id, p.id)
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == ids[0]))
            db.execute(delete(Request).where(Request.project_id == ids[1]))
            db.execute(delete(Project).where(Project.org_id == ids[0]))
            db.execute(delete(Organization).where(Organization.id == ids[0]))
            db.commit()


def test_every_metering_path_stamps_tokens(org_project):
    """record_usage / record_project_usage / record_org_usage all write the token
    count - a path that forgets leaves a hole in the graph that looks like idle
    time."""
    from app.services import llm

    org_id, pid = org_project
    usage = {"model": "mistral-medium-latest", "input_tokens": 1000, "output_tokens": 200}
    with SyncSession() as db:
        project = db.get(Project, pid)
        llm.record_usage(db, project, usage, "dev run")
        llm.record_project_usage(db, project, [usage, usage], "chat answer")
        llm.record_org_usage(db, org_id, [usage], "MCP knowledge query")
        db.commit()

    with SyncSession() as db:
        rows = db.execute(select(CreditTransaction.kind, CreditTransaction.tokens)
                          .where(CreditTransaction.org_id == org_id)).all()
    by_detail = sorted(r.tokens for r in rows)
    assert by_detail == [1200, 1200, 2400], f"missing token stamps: {rows}"


@pytest.mark.asyncio
async def test_series_is_dense_and_sums_the_window(org_project):
    """Days with no spend still appear, and the totals match the rows."""
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.api import usage as usage_api
    from app.core.config import settings

    org_id, pid = org_project
    now = utcnow()
    with SyncSession() as db:
        # today and 3 days ago; the days between must still show up as zeros
        db.add(CreditTransaction(org_id=org_id, project_id=pid, amount=-1.5,
                                 kind="consumption", detail="build", tokens=5000))
        db.add(CreditTransaction(org_id=org_id, project_id=pid, amount=-0.25,
                                 kind="mcp_query", detail="MCP knowledge query",
                                 tokens=800, created_at=now - timedelta(days=3)))
        # a non-spend row must never enter the series
        db.add(CreditTransaction(org_id=org_id, project_id=pid, amount=50.0,
                                 kind="topup", detail="card"))
        db.commit()

    eng = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(eng, class_=AsyncSession,
                                      expire_on_commit=False)() as db:
            project = await db.get(Project, pid)
            out = await usage_api.project_usage(days=7, project=project, db=db)
    finally:
        await eng.dispose()

    assert len(out["series"]) == 7, "one bucket per day, gaps included"
    assert out["totals"]["tokens"] == 5800
    assert out["totals"]["mcp_tokens"] == 800
    assert out["totals"]["credits"] == pytest.approx(1.75)  # topup excluded
    assert sum(1 for b in out["series"] if b["tokens"] == 0) == 5


@pytest.mark.asyncio
async def test_request_outcomes_bucket_done_vs_canceled(org_project):
    """§usage graph request outcomes: done and rejected ("canceled") requests
    land in their filing day's bucket; open/in-flight ones never count; the
    lifetime totals see past the window."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.api import usage as usage_api
    from app.core.config import settings
    from app.models import Request

    org_id, pid = org_project
    now = utcnow()
    with SyncSession() as db:
        db.add(Request(project_id=pid, title="shipped", type="feature",
                       handling="ai", status="done"))
        db.add(Request(project_id=pid, title="killed", type="bug",
                       handling="ai", status="rejected",
                       created_at=now - timedelta(days=2)))
        db.add(Request(project_id=pid, title="still open", type="bug",
                       handling="ai", status="open"))
        db.add(Request(project_id=pid, title="ancient win", type="feature",
                       handling="ai", status="done",
                       created_at=now - timedelta(days=30)))
        db.commit()

    eng = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(eng, class_=AsyncSession,
                                      expire_on_commit=False)() as db:
            project = await db.get(Project, pid)
            out = await usage_api.project_usage(days=7, project=project, db=db)
    finally:
        await eng.dispose()

    assert out["totals"]["requests_done"] == 1          # the window slice
    assert out["totals"]["requests_canceled"] == 1
    assert out["totals"]["lifetime_requests_done"] == 2  # the ancient win too
    assert out["totals"]["lifetime_requests_canceled"] == 1
    assert out["series"][-1]["requests_done"] == 1       # today's bucket
    assert sum(b["requests_canceled"] for b in out["series"]) == 1
    assert all("requests_done" in b and "requests_canceled" in b for b in out["series"])


def test_zero_never_renders_as_negative_zero(org_project):
    """Ledger amounts are negative debits, so display values are negations - and
    negating a zero sum yields -0.0, which the UI printed as "-0"."""
    from app.api.mcp_tokens import positive

    assert positive(-0.0) == 0.0 and str(positive(-0.0)) == "0.0"
    assert positive(-0.0000001) == 0.0          # rounds to zero, not "-0"
    assert positive(-1.23456789) == -1.2346     # real magnitudes untouched
    assert positive(2.5) == 2.5
