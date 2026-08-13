"""§MCP project tokens: the three token scopes and the walls between them.

A third scope was added to a surface the Scalevisor hub authenticates against,
so these tests pin BOTH directions: a project token gets its own reduced tool
list and can never name a sibling project, and the hub's contract - 21 hub tools,
no customer tool, its own backend guard - is exactly what it was.

The sidecar (mcp/main.py) is a standalone image; compose.dev mounts it read-only
at /app/mcp_src, so it loads by file path and skips where that mount is absent
(test_mcp_hub_timeouts does the same).
"""
import importlib.util
import pathlib
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import hash_api_token, new_api_token
from app.models import (ApiToken, CreditTransaction, Organization, Project,
                        StatusChange, User)

MCP_MAIN = pathlib.Path("/app/mcp_src/main.py")


@pytest.fixture(scope="module")
def mcp_main():
    if not MCP_MAIN.exists():
        pytest.skip("mcp sidecar source not mounted at /app/mcp_src")
    spec = importlib.util.spec_from_file_location("mcp_scopes_under_test", MCP_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def org_project():
    """A throwaway org + user + two projects (the second is the sibling a project
    token must never reach)."""
    with SyncSession() as db:
        org = Organization(name="MCP Scope Org", credit_balance=50.0)
        db.add(org)
        db.commit()
        user = User(org_id=org.id, email=f"mcp-{org.id[:8]}@example.org",
                    password_hash="x", role="customer", email_verified=True)
        db.add(user)
        a = Project(org_id=org.id, name="Mine", description="d", kind="auto_dev",
                    status="development", workspace_path="/tmp/mcp-a")
        b = Project(org_id=org.id, name="Sibling", description="d", kind="ai",
                    status="development", workspace_path="/tmp/mcp-b")
        db.add_all([a, b])
        db.commit()
        ids = (org.id, user.id, a.id, b.id)
    try:
        yield ids
    finally:
        with SyncSession() as db:
            oid = ids[0]
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            db.execute(delete(ApiToken).where(ApiToken.user_id == ids[1]))
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


# The app's async engine is shared and pooled per event loop; each async test
# here gets its own loop, so these open a private engine instead of borrowing
# connections another test opened in a loop that is already closed.
@asynccontextmanager
async def _fresh_session():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.core.config import settings
    eng = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(eng, class_=AsyncSession,
                                      expire_on_commit=False)() as db:
            yield db
    finally:
        await eng.dispose()


def _token_request(token: str):
    """The slice of Request the token deps touch."""
    class _Req:
        headers = {"authorization": f"Bearer {token}"}
        url = type("U", (), {"path": "/api/knowledge/answer"})()
        method = "POST"
        scope = {"route": None}
    return _Req()


# ---------------------------------------------------------------- tool lists

def test_each_scope_gets_its_own_tool_list(mcp_main):
    hub = {t["name"] for t in mcp_main.HUB_TOOLS}
    user = {t["name"] for t in mcp_main.USER_TOOLS}
    project = {t["name"] for t in mcp_main.PROJECT_TOOLS}

    # The hub's contract with the spoke - the tools scalevisor's spoke_client calls.
    for name in ("spoke_info", "usage_summary", "grant_credits", "create_org",
                 "create_project", "get_project", "project_action",
                 "list_project_messages", "post_project_message",
                 "list_project_requests", "create_project_request",
                 "start_project_request", "get_project_dev_activity",
                 "kb_leak_audit", "run_eval"):
        assert name in hub, f"hub tool {name} disappeared - scalevisor calls it"

    # No customer tool ever leaks into the hub list (a hub token must not bill).
    assert "search_knowledge" not in hub
    assert not (hub & user), "hub and user tool lists must stay disjoint"
    assert not (hub & project), "hub and project tool lists must stay disjoint"


def test_project_tools_cannot_name_another_project(mcp_main):
    """The token IS the project: no project_id input anywhere in its schemas."""
    for tool in mcp_main.PROJECT_TOOLS:
        props = tool["inputSchema"].get("properties", {})
        assert "project_id" not in props, f"{tool['name']} exposes project_id"
    # A project token is NOT simply a narrowed user token: it reads less (no
    # cross-project listing) but does more (§MCP delegate can spend build money),
    # so the read tools it shares stay a subset while the write tools are its own.
    project = {t["name"] for t in mcp_main.PROJECT_TOOLS}
    user = {t["name"] for t in mcp_main.USER_TOOLS}
    hub = {t["name"] for t in mcp_main.HUB_TOOLS}
    delegation = {"delegate_development", "get_delegation", "list_delegations",
                  "consult_codebase", "get_consult"}
    assert delegation <= project, "delegate + consult are the project scope's reason to exist"
    assert not (delegation & user), "a plain user token must not reach the build pipeline"
    assert not (delegation & hub), "the hub drives builds through its own tools"
    assert (project - delegation) <= user


def test_project_scope_never_falls_through_to_user_tools(mcp_main):
    """The routing added a third branch - a project token must NOT land in the
    user branch, which would hand it list_projects across the whole org."""
    assert "list_projects" not in mcp_main.PROJECT_TOOL_NAMES
    assert mcp_main.PROJECT_TOOL_NAMES != mcp_main.USER_TOOL_NAMES


def test_sidecar_auth_query_still_joins_user_and_reads_project(mcp_main):
    """The auth lookup inner-joins "user", so every scope needs a user_id; it
    must also select project_id or the project branch can't bind."""
    src = MCP_MAIN.read_text()
    assert 't.project_id' in src, "auth query must select the token's project"
    assert 'JOIN "user" u ON u.id = t.user_id' in src


# ---------------------------------------------------------------- backend guards

@pytest.mark.asyncio
async def test_backend_token_guards_by_scope(org_project):
    """require_api_token accepts customer scopes and refuses hub; the hub guard
    is the mirror image."""
    from fastapi import HTTPException
    from app.core import deps

    _, user_id, pid, _ = org_project
    tokens = {s: _mint(user_id, s, pid if s == "project" else None)
              for s in ("user", "project", "hub")}

    async with _fresh_session() as db:
        for scope in ("user", "project"):
            u = await deps.require_api_token(_token_request(tokens[scope]), db)
            assert u.id == user_id, f"{scope} token must authenticate"
        with pytest.raises(HTTPException) as exc:
            await deps.require_api_token(_token_request(tokens["hub"]), db)
        assert exc.value.status_code == 403

        # the hub guard still accepts ONLY hub
        u = await deps.require_hub_token(_token_request(tokens["hub"]), db)
        assert u.id == user_id
        for scope in ("user", "project"):
            with pytest.raises(HTTPException):
                await deps.require_hub_token(_token_request(tokens[scope]), db)


@pytest.mark.asyncio
async def test_project_token_resolves_its_project(org_project):
    from app.core import deps

    _, user_id, pid, _ = org_project
    plain = _mint(user_id, "project", pid)
    user_plain = _mint(user_id, "user")

    async with _fresh_session() as db:
        user, project = await deps.require_api_token_project(_token_request(plain), db)
        assert project is not None and project.id == pid
        # a plain user token binds no project - it keeps billing the org
        user2, project2 = await deps.require_api_token_project(
            _token_request(user_plain), db)
        assert project2 is None and user2.id == user_id


# ---------------------------------------------------------------- privacy

def test_a_query_is_never_written_to_the_ledger(org_project, monkeypatch):
    """§MCP privacy: the question is the caller's business - counters and cost
    are ours. Nothing in the ledger may echo it back."""
    from app.services import knowledge, llm, rag

    _, _, pid, _ = org_project
    secret = "how do I rotate the sovereign KMS keys for client ACME"

    monkeypatch.setattr(rag, "retrieve", lambda db, q, k, **kw: ([], []))
    monkeypatch.setattr(knowledge, "is_priced", lambda m: True)
    billed: list = []
    monkeypatch.setattr(llm, "record_project_usage",
                        lambda db, project, usages, detail: billed.append(("project", detail)) or 0.0)
    monkeypatch.setattr(llm, "record_org_usage",
                        lambda db, org_id, usages, detail, **kw: billed.append(("org", detail)) or 0.0)

    with SyncSession() as db:
        project = db.get(Project, pid)
        knowledge.answer_question(db, project.org_id, secret, 6, project=project)
        knowledge.answer_question(db, project.org_id, secret, 6)

    assert [b[0] for b in billed] == ["project", "org"], "a project token bills its project"
    for _, detail in billed:
        assert secret not in detail
        assert "rotate" not in detail.lower()
