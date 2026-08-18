"""§MCP projects: the lightweight project an MCP client works through.

Knowledge answers, codebase consults and delegated builds are all project-scoped
(services/knowledge.answer_question) - the project is what picks the model, what
narrows the knowledge bases, and what the spend lands on. A customer who only
wants to consult from their terminal still needs one, so `kind="mcp"` is that
project with none of the machinery the other kinds carry: no LLM evaluation, no
GitLab/workspace provisioning, no estimate or payment gate, no chat thread. It is
born live (`development`) because its token works the moment it exists.

Auto-development is blocked on it for the same reason it is blocked on chat and
direct-quote projects: nothing here was priced or approved as a build.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase, Project, StatusChange, User

KIND = "mcp"


async def default_kb_ids(db: AsyncSession) -> list[str]:
    """The KB selection a new MCP project starts with: a SNAPSHOT of the
    retrieval-capable sources that are live right now - the local KB if enabled,
    plus every enabled AND verified git source.

    Unlike the wizard kinds, an MCP project starts with its knowledge bases
    selected rather than empty: consulting them is the entire reason it exists,
    and a project that answers "I don't have anything on that" until someone
    visits its settings would make the one-click flow useless. It stays a
    snapshot, not a standing "all": a source added later does not silently join
    an existing project, and narrowing it afterwards is a checkbox on the
    project. The /admin/knowledge-bases kill-switch still cascades, because
    `rag.selected_root_keys` intersects any selection with what is enabled.
    """
    local = (await db.execute(select(KnowledgeBase.id).where(
        KnowledgeBase.kind == "local", KnowledgeBase.enabled.is_(True)))).scalars().all()
    git = (await db.execute(select(KnowledgeBase.id).where(
        KnowledgeBase.kind == "git",
        KnowledgeBase.enabled.is_(True),
        KnowledgeBase.verified.is_(True)))).scalars().all()
    return sorted({*local, *git})


async def create(db: AsyncSession, user: User, title: str,
                 description: str | None = None) -> Project:
    """Create an `mcp` project for this user's org. Caller commits.

    The title is the customer's own words and is kept verbatim - the §9.2
    "no name field" rule exists because the wizard derives a title from a long
    brief, and there is no brief here to derive one from.
    """
    project = Project(
        org_id=user.org_id,
        name=title.strip()[:255],
        kind=KIND,
        description=(description or "").strip(),
        from_scratch=True,
        block_auto_development=True,
        kb_ids=await default_kb_ids(db),
        status="development",
    )
    db.add(project)
    await db.flush()
    db.add(StatusChange(project_id=project.id, from_status=None, to_status="development",
                        actor="customer", reason="MCP project created"))
    return project
