"""§project defaults: the knowledge bases and tools a NEW project starts with.

Both gates are per-project and they default OPPOSITE ways: a knowledge base
reaches a project only if `Project.kb_ids` names it (opt-in), while a §Tools row
reaches every project unless a `ProjectToolConfig` row says otherwise (opt-out).
A wizard project therefore starts with no knowledge at all - right for a build
whose sources an admin curates, wrong for a `chat` project, whose entire product
is answering from the knowledge base: it opens, answers "I don't have anything on
that in the knowledge base", and only an admin visiting the project page can fix
it.

So the instance admin sets that starting selection PER KIND (/admin/settings):
`ai`, `auto_dev`, `direct_quote` and `chat` each get their own, because what a
build may read and what a conversation may read are not the same decision.

The stamp happens ONCE, at creation, and is an ordinary per-project selection
afterwards: editing the defaults never reaches back into projects that already
exist, and the per-project modals stay the truth for those.

Each map only ever NARROWS what the global lists allow. A defaulted KB id is
still subject to that KB being enabled and verified (`rag.selected_root_keys`),
and the tools map stores what to switch OFF rather than what to turn on - so a
tool row the admin adds later reaches new projects exactly as it does today, and
a defaulted tool never force-enables a globally disabled one. An id whose row was
deleted since is dropped at stamp time: a dangling `tool_id` would turn the
customer's create click into a foreign-key error.

The `mcp` kind is deliberately absent. An MCP project snapshots the live
retrieval sources at creation (`mcp_projects.default_kb_ids`) because consulting
them is the whole reason it exists.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase, Project, ProjectToolConfig, Tool
from app.services import app_settings

# {kind: [KnowledgeBase.id]} - the selection a new project of that kind is given.
KB_KEY = "default_kb_ids_by_kind"
# {kind: [Tool.id]} - the tools switched OFF for a new project of that kind
# (an explicit ProjectToolConfig.enabled=False row); everything else inherits.
TOOLS_OFF_KEY = "default_tools_off_by_kind"

# The wizard kinds. `mcp` is not one of them - see the module docstring.
KINDS = ("ai", "auto_dev", "direct_quote", "chat")


def _clean(raw) -> dict[str, list[str]]:
    """A stored map filled out to every kind, so callers never branch on a missing
    key. A kind that was never configured reads as [] - for knowledge bases that
    is today's behavior (opt-in default), and for tools it means "switch nothing
    off", also today's behavior."""
    stored = raw if isinstance(raw, dict) else {}
    out: dict[str, list[str]] = {}
    for kind in KINDS:
        ids = stored.get(kind)
        out[kind] = sorted({str(i) for i in ids}) if isinstance(ids, list) else []
    return out


async def describe(db: AsyncSession) -> dict:
    """Both maps, for the admin Settings payload."""
    return {
        "default_kb_ids": _clean(await app_settings.get_value(db, KB_KEY)),
        "default_tools_off": _clean(await app_settings.get_value(db, TOOLS_OFF_KEY)),
    }


def normalize(raw: dict[str, list[str]], known: set[str]) -> dict[str, list[str]]:
    """Validate an incoming map against the ids that exist. Raises ValueError.

    A sent map REPLACES the stored one (the page always sends every kind), so a
    kind omitted here is a kind with no defaults."""
    out: dict[str, list[str]] = {}
    for kind, ids in raw.items():
        if kind not in KINDS:
            raise ValueError(f"unknown project kind: {kind}")
        if not isinstance(ids, list):
            raise ValueError(f"{kind}: expected a list of ids")
        unknown = sorted({str(i) for i in ids} - known)
        if unknown:
            raise ValueError(f"{kind}: unknown id {unknown[0]}")
        out[kind] = sorted({str(i) for i in ids})
    return out


async def apply(db: AsyncSession, project: Project) -> None:
    """Stamp this instance's defaults for `project.kind` onto a project that has
    just been flushed (the tool overrides carry its id). Caller commits.

    Called from every wizard create path - the customer route and the hub
    pass-through - because the defaults belong to the instance whose knowledge is
    being read, not to whoever clicked create.
    """
    kb_ids = _clean(await app_settings.get_value(db, KB_KEY)).get(project.kind) or []
    tools_off = _clean(await app_settings.get_value(db, TOOLS_OFF_KEY)).get(project.kind) or []
    if kb_ids:
        live = set((await db.execute(select(KnowledgeBase.id).where(
            KnowledgeBase.id.in_(kb_ids)))).scalars().all())
        project.kb_ids = sorted(i for i in kb_ids if i in live)
    if tools_off:
        live = set((await db.execute(select(Tool.id).where(
            Tool.id.in_(tools_off)))).scalars().all())
        for tool_id in sorted(i for i in tools_off if i in live):
            db.add(ProjectToolConfig(project_id=project.id, tool_id=tool_id, enabled=False))
