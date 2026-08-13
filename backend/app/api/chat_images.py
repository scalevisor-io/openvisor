"""§chat images: uploading and serving images attached to chat messages.

Two-step by design, because the message endpoint takes JSON: upload here, get
ids back, then post the message with `image_ids`. An image uploaded but never
sent stays unlinked (`message_id is null`) and renders nowhere.

The model gate is enforced HERE, not only in the UI: `services/vision` decides
whether this project's model can read images, and an upload against a model that
can't is refused. A client that got the gate wrong still cannot create a message
the model will choke on.
"""
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, get_project_for_user
from app.models import ChatImage, Project, User
from app.services import vision

router = APIRouter(prefix="/api/projects/{project_id}/chat-images", tags=["chat"])

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PER_MESSAGE = 4
# What a vision model actually accepts, and what a browser will render back.
# Sniffed from the magic bytes, never from the client's content-type header -
# and hand-rolled because imghdr is deprecated and gone in Python 3.13.
def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_out(img: ChatImage) -> dict:
    return {"id": img.id, "filename": img.filename, "content_type": img.content_type,
            "size_bytes": img.size_bytes}


@router.post("", status_code=201)
async def upload_images(files: list[UploadFile],
                        project: Project = Depends(get_project_for_user),
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """Upload images for the next chat message. Returns ids to pass as
    `image_ids` when posting it."""
    support = await vision.project_image_support(db, project)
    if not support["enabled"]:
        # 409, not 403: nothing is wrong with the caller's rights - the project's
        # model just can't read images.
        raise HTTPException(409, support["reason"] or "This project's model can't read images")
    if not files:
        raise HTTPException(400, "No images provided")
    if len(files) > MAX_PER_MESSAGE:
        raise HTTPException(409, f"At most {MAX_PER_MESSAGE} images per message")

    author = "admin" if user.role == "admin" else "customer"
    out = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"Images are limited to "
                                     f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        # Trust the bytes, not the client's content-type header.
        mime = sniff_image(data)
        if mime is None:
            raise HTTPException(415, "Images must be PNG, JPEG, WebP or GIF")
        row = ChatImage(project_id=project.id, author=author,
                        filename=(f.filename or f"image.{mime.split('/')[1]}")[:255],
                        content_type=mime, size_bytes=len(data), data=data)
        db.add(row)
        out.append(row)
    await db.flush()
    payload = [image_out(i) for i in out]
    await db.commit()
    return payload


@router.get("/{image_id}")
async def get_image(image_id: str, project: Project = Depends(get_project_for_user),
                    db: AsyncSession = Depends(get_db)):
    """Serve the bytes. Access rides on the project dependency, so a share's
    viewer sees the thread's images and nobody else can."""
    img = await db.get(ChatImage, image_id)
    if img is None or img.project_id != project.id:
        raise HTTPException(404, "Image not found")
    return Response(content=img.data, media_type=img.content_type,
                    headers={"Cache-Control": "private, max-age=86400"})


async def link_to_message(db: AsyncSession, project: Project, message_id: str,
                          image_ids: list[str], author: str) -> list[dict]:
    """Attach freshly-uploaded images to the message that references them.

    Only unlinked images of THIS project by the SAME author can be claimed - so a
    message can never adopt someone else's picture, and a re-posted id can never
    move an image from one message to another (messages are immutable)."""
    if not image_ids:
        return []
    rows = (await db.execute(select(ChatImage).where(
        ChatImage.id.in_(image_ids[:MAX_PER_MESSAGE]),
        ChatImage.project_id == project.id,
        ChatImage.message_id.is_(None),
        ChatImage.author == author))).scalars().all()
    for row in rows:
        row.message_id = message_id
    return [image_out(r) for r in rows]
