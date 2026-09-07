"""§chat documents: uploading and serving documents attached to chat messages.

The twin of api/chat_images, two-step for the same reason (the message endpoint
takes JSON): upload here, get ids back, post the message with `document_ids`.
An uploaded-but-never-sent document stays unlinked and renders nowhere.

No vision gate: a document reaches the model as TEXT, extracted here at upload
(services/documents), so any model can read one. The gate that does apply is
readability - bytes that are not a supported document are 415, a supported
document with nothing readable in it (scanned or encrypted PDF) is 422 with the
reason, so the customer learns it now rather than from a model that answers as
if nothing was attached.

Serving is download-only: `Content-Disposition: attachment` + nosniff + a
sandboxing CSP, because an HTML file rendered inline from the app origin would
be stored XSS - a customer's upload must never execute as the app.
"""
import asyncio
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, get_project_for_user
from app.models import ChatDocument, Project, User
from app.services import documents

router = APIRouter(prefix="/api/projects/{project_id}/chat-documents", tags=["chat"])

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PER_MESSAGE = 4


def document_out(doc: ChatDocument) -> dict:
    return {"id": doc.id, "filename": doc.filename, "content_type": doc.content_type,
            "size_bytes": doc.size_bytes, "char_count": doc.char_count,
            "truncated": doc.truncated, "pages": doc.pages}


@router.post("", status_code=201)
async def upload_documents(files: list[UploadFile],
                           project: Project = Depends(get_project_for_user),
                           user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    """Upload documents for the next chat message. Returns ids to pass as
    `document_ids` when posting it."""
    if not await documents.enabled_async(db):
        # 409 like the image gate: nothing is wrong with the caller's rights.
        raise HTTPException(409, documents.DISABLED_REASON)
    if not files:
        raise HTTPException(400, "No documents provided")
    if len(files) > MAX_PER_MESSAGE:
        raise HTTPException(409, f"At most {MAX_PER_MESSAGE} documents per message")

    author = "admin" if user.role == "admin" else "customer"
    out = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_DOCUMENT_BYTES:
            raise HTTPException(413, f"Documents are limited to "
                                     f"{MAX_DOCUMENT_BYTES // (1024 * 1024)} MB")
        # Trust the bytes (and, for text, the filename), not the client's header.
        mime = documents.sniff_document(data, f.filename)
        if mime is None:
            raise HTTPException(415, "Documents must be PDF, Word (.docx), Markdown, HTML, "
                                     "CSV, JSON or plain text")
        try:
            # pypdf is CPU-bound: keep a 10 MB PDF off the event loop.
            text, pages = await asyncio.to_thread(documents.extract_text, data, mime)
        except documents.DocumentError as exc:
            raise HTTPException(422, f"{f.filename or 'document'}: {exc}")
        # A label, never a path - but it is echoed into the task file and the
        # download header, so control characters go.
        name = re.sub(r"[\x00-\x1f\x7f]+", " ", f.filename or "").strip()[:255] \
            or f"document.{documents.EXTENSIONS.get(mime, 'txt')}"
        row = ChatDocument(project_id=project.id, author=author, filename=name,
                           content_type=mime, size_bytes=len(data), data=data, text=text,
                           char_count=len(text),
                           truncated=len(text) >= documents.MAX_TEXT_CHARS, pages=pages)
        db.add(row)
        out.append(row)
    await db.flush()
    payload = [document_out(d) for d in out]
    await db.commit()
    return payload


@router.get("/{document_id}")
async def get_document(document_id: str, project: Project = Depends(get_project_for_user),
                       db: AsyncSession = Depends(get_db)):
    """Serve the original bytes as a DOWNLOAD. Access rides on the project
    dependency, so a share's viewer sees the thread's documents and nobody else."""
    doc = await db.get(ChatDocument, document_id)
    if doc is None or doc.project_id != project.id:
        raise HTTPException(404, "Document not found")
    # HTML and friends go out as opaque bytes: never a type a browser would render.
    media = doc.content_type if doc.content_type in (documents.PDF, documents.DOCX) \
        else "application/octet-stream"
    return Response(content=doc.data, media_type=media, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc.filename)}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "private, max-age=86400"})


async def link_to_message(db: AsyncSession, project: Project, message_id: str,
                          document_ids: list[str], author: str) -> list[dict]:
    """Attach freshly-uploaded documents to the message that references them -
    the same claim rules as images: only unlinked documents of THIS project by
    the SAME author, so a message can never adopt someone else's file and a
    re-posted id can never move one between (immutable) messages."""
    if not document_ids:
        return []
    rows = (await db.execute(select(ChatDocument).where(
        ChatDocument.id.in_(document_ids[:MAX_PER_MESSAGE]),
        ChatDocument.project_id == project.id,
        ChatDocument.message_id.is_(None),
        ChatDocument.author == author))).scalars().all()
    for row in rows:
        row.message_id = message_id
    return [document_out(r) for r in rows]
