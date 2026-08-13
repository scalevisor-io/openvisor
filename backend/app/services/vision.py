"""§chat images: can THIS project's model read an image?

One verdict function, two thin resolvers (async for the API, sync for the
workers), mirroring `_project_model_config`'s order - saved endpoint → legacy
inline config → instance default - so the answer the chat box shows and the
answer the worker acts on can never disagree.

The verdict is tri-state at the source and binary at the surface: enabled only
when something has positively said yes. "Nobody has checked" and "the model said
no" both disable the attach button, but they say different things in the
tooltip, because one of them is fixable by pressing Test.

There is no capability-discovery API in the OpenAI-compatible contract
(`/models` returns ids, not capabilities), which is why the answer has to be
STORED: from the endpoint Test probe, or from an admin who declared it.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AppSetting, ModelEndpoint, Project, ProjectModelConfig

# AppSetting key for the instance-default model (the env-configured OPENAI_MODEL
# has no ModelEndpoint row to carry the flag).
DEFAULT_MODEL_IMAGES_KEY = "default_model_supports_images"


def _verdict(endpoint: ModelEndpoint | None, inline_model: str | None,
             default_enabled: bool) -> dict:
    """`{enabled, reason, model}` - `reason` is the tooltip on the disabled attach
    button, written for the person who reads it, not for a log."""
    if endpoint is not None and endpoint.model_name:
        m = endpoint.model_name
        if endpoint.supports_images:
            return {"enabled": True, "reason": None, "model": m}
        if endpoint.supports_images is False:
            return {"enabled": False, "model": m,
                    "reason": f"{m} can't read images - an admin can point this project "
                              "at a model that can."}
        return {"enabled": False, "model": m,
                "reason": f"{m} hasn't been checked for image support yet - an admin can "
                          "test it on the Model configuration page."}

    if inline_model:
        # Legacy inline config: no row to carry a verdict, so nothing has said yes.
        return {"enabled": False, "model": inline_model,
                "reason": f"{inline_model} is configured inline and hasn't been checked for "
                          "image support - an admin can move it to a saved model endpoint "
                          "and test it."}

    if default_enabled:
        return {"enabled": True, "reason": None, "model": settings.openai_model}
    return {"enabled": False, "model": settings.openai_model,
            "reason": f"The default model ({settings.openai_model}) hasn't been confirmed to "
                      "read images - an admin can enable it on the Model configuration page."}


async def project_image_support(db: AsyncSession, project: Project) -> dict:
    row = (await db.execute(select(ProjectModelConfig).where(
        ProjectModelConfig.project_id == project.id))).scalar_one_or_none()
    endpoint = (await db.get(ModelEndpoint, row.endpoint_id)
                if row is not None and row.endpoint_id else None)
    setting = await db.get(AppSetting, DEFAULT_MODEL_IMAGES_KEY)
    return _verdict(endpoint, row.model_name if row is not None and not row.endpoint_id else None,
                    bool(setting.value) if setting is not None else False)


def project_image_support_sync(db: Session, project: Project) -> dict:
    """The workers' twin - same verdict, sync session."""
    row = db.query(ProjectModelConfig).filter_by(project_id=project.id).first()
    endpoint = (db.get(ModelEndpoint, row.endpoint_id)
                if row is not None and row.endpoint_id else None)
    setting = db.get(AppSetting, DEFAULT_MODEL_IMAGES_KEY)
    return _verdict(endpoint, row.model_name if row is not None and not row.endpoint_id else None,
                    bool(setting.value) if setting is not None else False)
