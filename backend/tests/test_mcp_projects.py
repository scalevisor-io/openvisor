"""§MCP projects: the project an MCP client works through, and who may create one.

Two doors reach the same service, and the walls differ: the SPA's one-click flow
hands back a token, the MCP `create_project` tool never does, and a project-scoped
token cannot create a sibling through either.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import new_api_token
from app.main import app
from app.models import (ApiToken, CreditTransaction, KnowledgeBase, Organization,
                        Project, StatusChange, User)
from app.services import mcp_projects


@pytest.fixture
def org_user():
    with SyncSession() as db:
        org = Organization(name="MCP Project Org", credit_balance=50.0)
        db.add(org)
        db.commit()
        user = User(org_id=org.id, email=f"mcpp-{org.id[:8]}@example.org",
                    password_hash="x", role="customer", email_verified=True)
        db.add(user)
        db.commit()
        ids = (org.id, user.id)
    try:
        yield ids
    finally:
        with SyncSession() as db:
            oid, uid = ids
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            db.execute(delete(ApiToken).where(ApiToken.user_id == uid))
            if pids:
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _mint(user_id: str, scope: str, project_id: str | None = None) -> str:
    plaintext, token_hash = new_api_token()
    with SyncSession() as db:
        db.add(ApiToken(user_id=user_id, token_hash=token_hash, name=scope,
                        scope=scope, project_id=project_id))
        db.commit()
    return plaintext


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    """ONE client for the module, over a freshly pooled engine.

    The app's async engine pools asyncpg connections per event loop, and every
    TestClient runs its own loop - so a client inherits whatever an earlier test
    module left in the pool and dies with "attached to a different loop" on the
    first query. `dispose(close=False)` swaps in a clean pool while ABANDONING
    (never closing) the old connections, which is the only safe move: closing
    them is itself a cross-loop operation.
    """
    import asyncio

    from app.core.db import engine

    asyncio.run(engine.dispose(close=False))
    with TestClient(app) as c:
        yield c
    asyncio.run(engine.dispose(close=False))


def test_tool_door_creates_a_live_project_without_a_token(org_user, client):
    """The agent gets a project it can be pointed at - and no credential, which
    would otherwise land in a chat transcript."""
    _, uid = org_user
    token = _mint(uid, "user")
    r = client.post("/api/mcp/projects",
                    json={"title": "Terminal consulting", "description": "KB from my editor"},
                    headers=_auth(token))
    assert r.status_code == 201, r.text
    body = r.json()
    assert "token" not in body, "an MCP tool must never mint a long-lived credential"
    assert body["status"] == "development", "its token works the moment it exists"
    with SyncSession() as db:
        p = db.get(Project, body["project_id"])
        assert p.kind == "mcp"
        assert p.name == "Terminal consulting", "the title is kept verbatim"
        assert p.block_auto_development is True, "nothing here was priced as a build"


def test_a_project_token_cannot_create_a_sibling(org_user, client):
    """The whole point of the project scope: the token IS the project."""
    oid, uid = org_user
    with SyncSession() as db:
        p = Project(org_id=oid, name="Bound", description="d", kind="mcp",
                    status="development")
        db.add(p)
        db.commit()
        pid = p.id
    token = _mint(uid, "project", project_id=pid)
    r = client.post("/api/mcp/projects", json={"title": "Sibling"}, headers=_auth(token))
    assert r.status_code == 403


def test_hub_token_is_refused(org_user, client):
    """A hub token drives the /api/hub surface, never a customer's own projects."""
    _, uid = org_user
    token = _mint(uid, "hub")
    r = client.post("/api/mcp/projects", json={"title": "Nope"}, headers=_auth(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_new_project_starts_with_the_live_knowledge_bases_selected():
    """Consulting the KB is why an MCP project exists, so it must not start deaf -
    but as a snapshot, so a source added later never silently joins it."""
    # The app's async engine is pooled per event loop, so open a private one
    # rather than borrow a connection from a loop another test already closed.
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.config import settings

    eng = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(eng, class_=AsyncSession,
                                      expire_on_commit=False)() as db:
            ids = await mcp_projects.default_kb_ids(db)
            live = (await db.execute(select(KnowledgeBase.id).where(
                KnowledgeBase.kind == "local",
                KnowledgeBase.enabled.is_(True)))).scalars().all()
    finally:
        await eng.dispose()

    assert isinstance(ids, list)
    for kb_id in live:
        assert kb_id in ids, "an enabled local KB must be selected for a new MCP project"
