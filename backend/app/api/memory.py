from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, get_project_for_user
from app.core.encryption import decrypt, encrypt
from app.models import Organization, Project, ProjectMemory, User
from app.schemas.schemas import MemoryIn, ProjectMemorySettingsIn

router = APIRouter(prefix="/api/projects/{project_id}/memory", tags=["memory"])


def _entry_out(e: ProjectMemory) -> dict:
    """Values are always returned in clear (they stay envelope-encrypted at
    rest): is_secret only drives display masking in the UI. Customer, admin,
    and the dev agent can all retrieve/copy them."""
    return {"id": e.id, "key": e.key, "is_secret": e.is_secret, "author": e.author,
            "description": e.description or "", "updated_at": e.updated_at,
            "value": decrypt(e.value_enc)}


async def _settings_out(project: Project, db: AsyncSession) -> dict:
    """Resolve the project's global-memory setting: the raw override plus the org
    default and the effective value (override wins; null → follow org default)."""
    org = await db.get(Organization, project.org_id)
    org_default = bool(org.global_memory_enabled_default) if org else True
    override = project.use_global_memory
    return {"use_global_memory": override, "org_default": org_default,
            "effective": org_default if override is None else override}


@router.get("/settings")
async def get_memory_settings(project: Project = Depends(get_project_for_user),
                              db: AsyncSession = Depends(get_db)):
    return await _settings_out(project, db)


@router.put("/settings")
async def set_memory_settings(body: ProjectMemorySettingsIn,
                              project: Project = Depends(get_project_for_user),
                              db: AsyncSession = Depends(get_db)):
    project.use_global_memory = body.use_global_memory
    await db.commit()
    return await _settings_out(project, db)


@router.get("")
async def list_memory(project: Project = Depends(get_project_for_user),
                      db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ProjectMemory).where(
        ProjectMemory.project_id == project.id).order_by(ProjectMemory.key))).scalars().all()
    return [_entry_out(e) for e in rows]


@router.put("")
async def upsert_memory(body: MemoryIn,
                        project: Project = Depends(get_project_for_user),
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    author = "admin" if user.role == "admin" else "customer"
    existing = (await db.execute(select(ProjectMemory).where(
        ProjectMemory.project_id == project.id,
        ProjectMemory.key == body.key))).scalar_one_or_none()
    if existing:
        existing.value_enc = encrypt(body.value)
        existing.is_secret = body.is_secret
        existing.description = body.description
        existing.author = author
        entry = existing
    else:
        entry = ProjectMemory(project_id=project.id, author=author, key=body.key,
                              value_enc=encrypt(body.value), is_secret=body.is_secret,
                              description=body.description)
        db.add(entry)
    await db.commit()
    return _entry_out(entry)


@router.delete("/{entry_id}")
async def delete_memory(entry_id: str,
                        project: Project = Depends(get_project_for_user),
                        db: AsyncSession = Depends(get_db)):
    entry = await db.get(ProjectMemory, entry_id)
    if entry is None or entry.project_id != project.id:
        raise HTTPException(404, "Not found")
    await db.delete(entry)
    await db.commit()
    return {"ok": True}
