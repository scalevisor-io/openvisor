"""Which model a project's LLM calls run on.

One resolver, shared by the dev pipeline / chat worker and by the billable
knowledge path, so a project's model choice means the same thing everywhere:
a saved `ModelEndpoint` first (its credential rotates in one place), then the
legacy inline `openai_*` columns, then the instance default for the project's
KIND (§project defaults), then the instance default from env.

The kind default is resolved on every call rather than stamped onto the project
at creation - unlike the knowledge bases and tools of §project defaults, which
ARE the per-project state and so must be written once. A model has a resolution
chain already, so the kind is one more link in it: switch the model a `chat`
should run on and every chat that never picked its own follows, which is the
point of setting it per kind (a conversation is billed per answer and does not
want the model a build wants). A project that chose its own endpoint keeps it.

Everything that depends on WHICH endpoint answers - the reasoning effort, the
§chat images verdict, the model a usage row is billed under - goes through
`project_endpoint` so they can never disagree about it.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption import decrypt
from app.models import AppSetting, ModelEndpoint, Project, ProjectModelConfig

# {kind: ModelEndpoint.id} - the endpoint a project of that kind runs on when it
# has not been given one of its own (admin Settings, §project defaults). A kind
# that is absent, or whose endpoint was deleted since, falls through to the env
# default exactly as before.
KIND_DEFAULT_KEY = "default_model_endpoint_by_kind"


def kind_defaults(raw) -> dict[str, str | None]:
    """A stored map, tolerant of anything that is not one (never written by us,
    but this is read on every LLM call)."""
    stored = raw if isinstance(raw, dict) else {}
    return {k: (str(v) if v else None) for k, v in stored.items()}


def normalize_kind_defaults(raw: dict, kinds, known: set[str]) -> dict[str, str]:
    """Validate an incoming map against the kinds and the endpoint ids that exist.
    An empty/None value clears that kind. Raises ValueError."""
    out: dict[str, str] = {}
    for kind, endpoint_id in raw.items():
        if kind not in kinds:
            raise ValueError(f"unknown project kind: {kind}")
        if not endpoint_id:
            continue
        if str(endpoint_id) not in known:
            raise ValueError(f"{kind}: unknown model endpoint {endpoint_id}")
        out[kind] = str(endpoint_id)
    return out


def _kind_endpoint_id(raw, kind: str) -> str | None:
    return kind_defaults(raw).get(kind)


def _chosen(row: ProjectModelConfig | None):
    """What the PROJECT itself chose: ('endpoint', id), ('inline', row) or None.
    A row left empty by a deleted endpoint (`ondelete SET NULL`) reads as no
    choice, so it falls through to the kind default like a project with no row."""
    if row is None:
        return None
    if row.endpoint_id:
        return ("endpoint", row.endpoint_id)
    if row.openai_base_url and row.openai_api_key_enc and row.model_name:
        return ("inline", row)
    return None


def project_endpoint(db: Session, project: Project
                     ) -> tuple[ModelEndpoint | None, ProjectModelConfig | None]:
    """`(endpoint, legacy_inline_row)` - what this project's calls route through.

    Exactly one of the two is set, or neither (the env default). An endpoint
    without a `model_name` is unusable and is skipped like a missing one.
    """
    chosen = _chosen(db.query(ProjectModelConfig).filter_by(project_id=project.id).first())
    if chosen is not None:
        if chosen[0] == "inline":
            return None, chosen[1]
        ep = db.get(ModelEndpoint, chosen[1])
        if ep is not None and ep.model_name:
            return ep, None
        return None, None  # an endpoint that vanished mid-flight: env default
    row = db.get(AppSetting, KIND_DEFAULT_KEY)
    eid = _kind_endpoint_id(row.value if row is not None else None, project.kind)
    ep = db.get(ModelEndpoint, eid) if eid else None
    return (ep if ep is not None and ep.model_name else None), None


async def project_endpoint_async(db: AsyncSession, project: Project
                                 ) -> tuple[ModelEndpoint | None, ProjectModelConfig | None]:
    """The API's twin of `project_endpoint` - same order, async session."""
    chosen = _chosen((await db.execute(select(ProjectModelConfig).where(
        ProjectModelConfig.project_id == project.id))).scalar_one_or_none())
    if chosen is not None:
        if chosen[0] == "inline":
            return None, chosen[1]
        ep = await db.get(ModelEndpoint, chosen[1])
        if ep is not None and ep.model_name:
            return ep, None
        return None, None
    row = await db.get(AppSetting, KIND_DEFAULT_KEY)
    eid = _kind_endpoint_id(row.value if row is not None else None, project.kind)
    ep = await db.get(ModelEndpoint, eid) if eid else None
    return (ep if ep is not None and ep.model_name else None), None


def project_model_config(db: Session, project: Project) -> tuple[str, str, str]:
    """(base_url, api_key, model) for this project's calls - see `project_endpoint`
    for the order."""
    endpoint, inline = project_endpoint(db, project)
    if endpoint is not None:
        return endpoint.base_url, decrypt(endpoint.api_key_enc), endpoint.model_name
    if inline is not None:
        return inline.openai_base_url, decrypt(inline.openai_api_key_enc), inline.model_name
    return settings.openai_base_url, settings.openai_api_key, settings.openai_model


def project_model_name(db: Session, project: Project) -> str:
    """Just the model this project runs, without decrypting a credential to get it."""
    endpoint, inline = project_endpoint(db, project)
    if endpoint is not None:
        return endpoint.model_name
    if inline is not None:
        return inline.model_name
    return settings.openai_model


async def project_model_name_async(db: AsyncSession, project: Project) -> str:
    """The API's twin of `project_model_name` - same order, async session."""
    endpoint, inline = await project_endpoint_async(db, project)
    if endpoint is not None:
        return endpoint.model_name
    if inline is not None:
        return inline.model_name
    return settings.openai_model
