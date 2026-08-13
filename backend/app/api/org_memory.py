from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.encryption import decrypt, encrypt
from app.models import Organization, OrgMemory, User
from app.schemas.schemas import MemoryIn, OrgMemorySettingsIn

# Global (organization-scoped) Memory shared across all the org's projects. Any
# member of the org can manage it - same access model as per-project Memory - and
# it is scoped to the caller's own org (user.org_id); values stay envelope-encrypted
# at rest and are returned in clear exactly like per-project Memory (§ global memory).
router = APIRouter(prefix="/api/org-memory", tags=["org-memory"])


def _entry_out(e: OrgMemory) -> dict:
    return {"id": e.id, "key": e.key, "is_secret": e.is_secret, "author": e.author,
            "description": e.description or "", "updated_at": e.updated_at,
            "value": decrypt(e.value_enc)}


@router.get("/settings")
async def get_settings(user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    org = await db.get(Organization, user.org_id)
    return {"enabled_default": bool(org.global_memory_enabled_default) if org else True}


@router.put("/settings")
async def set_settings(body: OrgMemorySettingsIn,
                       user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    org = await db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(404, "Organization not found")
    org.global_memory_enabled_default = body.enabled_default
    await db.commit()
    return {"enabled_default": org.global_memory_enabled_default}


@router.get("")
async def list_memory(user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(OrgMemory).where(
        OrgMemory.org_id == user.org_id).order_by(OrgMemory.key))).scalars().all()
    return [_entry_out(e) for e in rows]


@router.put("")
async def upsert_memory(body: MemoryIn,
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    author = "admin" if user.role == "admin" else "customer"
    existing = (await db.execute(select(OrgMemory).where(
        OrgMemory.org_id == user.org_id,
        OrgMemory.key == body.key))).scalar_one_or_none()
    if existing:
        existing.value_enc = encrypt(body.value)
        existing.is_secret = body.is_secret
        existing.description = body.description
        existing.author = author
        entry = existing
    else:
        entry = OrgMemory(org_id=user.org_id, author=author, key=body.key,
                          value_enc=encrypt(body.value), is_secret=body.is_secret,
                          description=body.description)
        db.add(entry)
    await db.commit()
    return _entry_out(entry)


@router.delete("/{entry_id}")
async def delete_memory(entry_id: str,
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    entry = await db.get(OrgMemory, entry_id)
    if entry is None or entry.org_id != user.org_id:
        raise HTTPException(404, "Not found")
    await db.delete(entry)
    await db.commit()
    return {"ok": True}
