"""§Tools `email`: the internal routes the email sidecar relays a build's tool
calls to.

Mounted under `/internal`, which neither Traefik nor the SPA's nginx forwards
(both route `/api` and `/ws` only), so these answer the sidecar on the internal
network and nobody on the internet. The Bearer is the per-project run token
the worker minted into the build's mcp.json (`services/email_tool.mint_token`):
no session, no CSRF.

Everything that decides whether mail goes out is re-read HERE on every call,
never trusted from the token or the caller: the row must still be configured
and enabled for the project, every recipient must be the project's customer or
on the admin's allowlist, and the project's hourly quota must have room. An
admin who disables the tool stops a running build's next send.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import rate_limit
from app.models import Project, Tool
from app.schemas.schemas import EmailToolSendIn
from app.services import email_tool
from app.services.sysmsg import post_system_message

log = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/email-tool", tags=["email-tool"])


async def _authorize(request: Request, db: AsyncSession) -> tuple[Project, Tool]:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    project_id = email_tool.read_token(token)
    if project_id is None:
        raise HTTPException(401, "This build's email credential is missing or expired")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(401, "This build's email credential is missing or expired")
    tool = (await db.execute(select(Tool).where(Tool.kind == email_tool.KIND)
                             .order_by(Tool.created_at))).scalars().first()
    if (tool is None or not email_tool.is_configured(tool)
            or not await email_tool.effective_enabled(db, tool, project.id)):
        raise HTTPException(403, "The email tool is turned off for this project")
    return project, tool


@router.get("/recipients")
async def list_recipients(request: Request, db: AsyncSession = Depends(get_db)):
    project, tool = await _authorize(request, db)
    return {"recipients": await email_tool.recipients(db, project, tool)}


@router.post("/send")
async def send(body: EmailToolSendIn, request: Request, db: AsyncSession = Depends(get_db)):
    project, tool = await _authorize(request, db)
    to = []
    for raw in body.to:
        addr = email_tool.normalize_address(raw)
        if addr is None:
            raise HTTPException(422, f"Not an email address: {raw.strip()[:80]}")
        if addr not in to:
            to.append(addr)
    allowed = {r["address"] for r in await email_tool.recipients(db, project, tool)}
    refused = [a for a in to if a not in allowed]
    if refused:
        raise HTTPException(403, "Not an approved recipient: " + ", ".join(refused)
                            + ". Call list_recipients for the addresses this project may email.")
    # Counted only once the message is otherwise sendable, so a refused address
    # does not eat the project's quota.
    await rate_limit(request, "email_tool", email_tool.SENDS_PER_HOUR, 3600,
                     identity=project.id)
    subject = " ".join(body.subject.split())
    text = (f"{body.body.rstrip()}\n\n"
            f"Sent by the development agent working on {project.name}.")
    try:
        await run_in_threadpool(email_tool.send, tool, to, subject, text)
    except email_tool.EmailToolError as exc:
        log.warning("email tool send failed for project %s: %s", project.id, exc)
        raise HTTPException(502, f"The email was not sent: {exc}") from None
    log.info("email tool: project %s sent a message to %d recipient(s)", project.id, len(to))
    # The customer sees every message their build sent, in the project's own
    # chat - immutable, like every message.
    await post_system_message(
        db, project.id,
        f'The development agent sent an email to {", ".join(to)}: "{subject}"')
    await db.commit()
    return {"ok": True, "sent_to": to}
