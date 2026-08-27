"""§consultant photo: the admin's portrait, shown on the public landing next to
CONSULTANT_NAME (the hero signature under the call to action, and the face of
the Direct quote card).

Never a repo asset: the landing is a static build shared by every deployment,
so the picture is an admin upload stored in `brand_asset` and served by the
public `GET /api/meta/consultant-photo`. The landing reveals its photo slots
only when that route answers 200, so an instance that never uploaded one
renders exactly as before. Served as uploaded - the api image carries no
imaging library, hence the size cap and the "square, at least 320 px" advice on
the Settings page.
"""
import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BrandAsset
from app.services.images import sniff_image

KEY = "consultant_photo"
MAX_BYTES = 1024 * 1024
# A portrait: no GIF - an animated avatar has no place on a consulting landing.
ALLOWED = {"image/png", "image/jpeg", "image/webp"}


class PhotoError(ValueError):
    """An upload the platform refuses; `status` is the HTTP code to answer with."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def meta(row: BrandAsset | None) -> dict | None:
    """What the admin page needs about the stored photo - never the bytes."""
    if row is None:
        return None
    return {
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "sha256": row.sha256,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def get(db: AsyncSession) -> BrandAsset | None:
    """The stored photo, bytes included (the public route)."""
    return await db.get(BrandAsset, KEY)


async def describe(db: AsyncSession) -> dict | None:
    """The photo's metadata without loading its bytes (the settings payload)."""
    row = (await db.execute(
        select(BrandAsset.content_type, BrandAsset.size_bytes, BrandAsset.sha256,
               BrandAsset.updated_at).where(BrandAsset.key == KEY))).first()
    if row is None:
        return None
    content_type, size_bytes, sha256, updated_at = row
    return {
        "content_type": content_type,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


async def put(db: AsyncSession, data: bytes) -> BrandAsset:
    """Store (or replace) the photo. The bytes decide what it is: the client's
    content type is never consulted. Caller commits."""
    if len(data) > MAX_BYTES:
        raise PhotoError(413, f"The photo is limited to {MAX_BYTES // 1024} KB")
    mime = sniff_image(data)
    if mime not in ALLOWED:
        raise PhotoError(415, "The photo must be a PNG, JPEG or WebP image")
    row = await db.get(BrandAsset, KEY)
    if row is None:
        row = BrandAsset(key=KEY)
        db.add(row)
    row.content_type = mime
    row.size_bytes = len(data)
    row.sha256 = hashlib.sha256(data).hexdigest()
    row.data = data
    await db.flush()
    return row


async def delete(db: AsyncSession) -> bool:
    """Remove the photo; the landing falls back to its photo-less layout. Caller
    commits. False when there was none (removing twice is not an error)."""
    row = await db.get(BrandAsset, KEY)
    if row is None:
        return False
    await db.delete(row)
    return True
