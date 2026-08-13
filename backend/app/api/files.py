"""Project files (Memory & files tab): customer-imported files the dev agent can
use. Stored in-DB like quote attachments (alpha scale); the worker stages them into
each dev sandbox at /workspace/.openvisor/files/<filename> and lists them in the
agent task. Not for secrets - Memory covers those (encrypted, exported as env vars)."""
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, get_project_for_user
from app.models import Project, ProjectFile, User

router = APIRouter(prefix="/api/projects/{project_id}/files", tags=["files"])

MAX_FILE_BYTES = 15 * 1024 * 1024  # per file; QuoteAttachment parity
MAX_FILES_PER_PROJECT = 20


def _safe_name(raw: str | None) -> str:
    """Bare filename only: it becomes a path segment under .openvisor/files/ in the
    sandbox, so any directory component is rejected rather than silently stripped."""
    name = (raw or "").strip()
    if not name or "\x00" in name or PurePosixPath(name.replace("\\", "/")).name != name \
            or name in {".", ".."}:
        raise HTTPException(400, f"Invalid filename: {raw!r}")
    if len(name) > 255:
        raise HTTPException(400, f"{name[:50]}…: filename is limited to 255 characters")
    return name


def _file_out(f: ProjectFile) -> dict:
    return {"id": f.id, "filename": f.filename, "content_type": f.content_type,
            "size_bytes": f.size_bytes, "author": f.author, "updated_at": f.updated_at}


@router.get("")
async def list_files(project: Project = Depends(get_project_for_user),
                     db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ProjectFile).where(ProjectFile.project_id == project.id)
        .order_by(ProjectFile.filename))).scalars().all()
    return [_file_out(f) for f in rows]


@router.post("", status_code=201)
async def upload_files(files: list[UploadFile],
                       project: Project = Depends(get_project_for_user),
                       user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    """Import one or several files; an existing filename is replaced in place."""
    if not files:
        raise HTTPException(400, "No files provided")
    author = "admin" if user.role == "admin" else "customer"
    existing = {f.filename: f for f in (await db.execute(
        select(ProjectFile).where(ProjectFile.project_id == project.id))).scalars().all()}
    new_names = {_safe_name(f.filename) for f in files} - set(existing)
    if len(existing) + len(new_names) > MAX_FILES_PER_PROJECT:
        raise HTTPException(409, f"A project holds at most {MAX_FILES_PER_PROJECT} files")
    out = []
    for f in files:
        name = _safe_name(f.filename)
        data = await f.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"{name}: files are limited to "
                                     f"{MAX_FILE_BYTES // (1024 * 1024)} MB")
        row = existing.get(name)
        if row is None:
            row = ProjectFile(project_id=project.id, filename=name)
            db.add(row)
            existing[name] = row
        row.author = author
        row.content_type = f.content_type or "application/octet-stream"
        row.size_bytes = len(data)
        row.data = data
        out.append(row)
    await db.flush()
    payload = [_file_out(f) for f in out]
    await db.commit()
    return payload


@router.get("/{file_id}")
async def download_file(file_id: str,
                        project: Project = Depends(get_project_for_user),
                        db: AsyncSession = Depends(get_db)):
    f = await db.get(ProjectFile, file_id)
    if f is None or f.project_id != project.id:
        raise HTTPException(404, "File not found")
    # Header values are latin-1; a unicode filename rides in the RFC 5987 form
    # with a plain-ASCII fallback.
    ascii_name = f.filename.encode("ascii", "ignore").decode().replace('"', "") or "file"
    return Response(content=f.data, media_type=f.content_type, headers={
        "Content-Disposition": f'attachment; filename="{ascii_name}"; '
                               f"filename*=UTF-8''{quote(f.filename)}"})


@router.delete("/{file_id}")
async def delete_file(file_id: str,
                      project: Project = Depends(get_project_for_user),
                      db: AsyncSession = Depends(get_db)):
    f = await db.get(ProjectFile, file_id)
    if f is None or f.project_id != project.id:
        raise HTTPException(404, "File not found")
    await db.delete(f)
    await db.commit()
    return {"ok": True}
