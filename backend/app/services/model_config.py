"""Which model a project's LLM calls run on.

One resolver, shared by the dev pipeline / chat worker and by the billable
knowledge path, so a project's model choice means the same thing everywhere:
a saved `ModelEndpoint` first (its credential rotates in one place), then the
legacy inline `openai_*` columns, then the instance default from env.
"""
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption import decrypt
from app.models import ModelEndpoint, Project, ProjectModelConfig


def project_model_config(db: Session, project: Project) -> tuple[str, str, str]:
    """(base_url, api_key, model) - the per-project override if set, else global.
    A saved ModelEndpoint (endpoint_id) is preferred so its credential rotates in
    one place; the legacy inline openai_* columns are the fallback for rows created
    before saved endpoints; an endpoint that was deleted out from under a row
    (endpoint_id nulled) degrades to the global default."""
    row = db.query(ProjectModelConfig).filter_by(project_id=project.id).first()
    if row:
        if row.endpoint_id:
            ep = db.get(ModelEndpoint, row.endpoint_id)
            if ep and ep.model_name:
                return ep.base_url, decrypt(ep.api_key_enc), ep.model_name
        elif row.openai_base_url and row.openai_api_key_enc and row.model_name:
            return row.openai_base_url, decrypt(row.openai_api_key_enc), row.model_name
    return settings.openai_base_url, settings.openai_api_key, settings.openai_model
