"""Admin management of the instance's MCP Tools (§Tools).

Tools ACT (GitHub/GitLab PR, issue and review operations over MCP); knowledge
bases inform. One global list of seeded rows (github, gitlab - disabled until
configured; the GitLab URL is editable for self-hosted instances,
https://<host>/api/v4/mcp) plus per-project overrides: tri-state enable, a URL
override (a customer's own GitLab instance) and a key override. Keys are
envelope-encrypted and NEVER returned (has_api_key only). Enabling a tool -
globally or per project - re-runs the §KB tool-poisoning scan server-side and
409s on findings (never trust the client)."""
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import require_admin
from app.core.encryption import decrypt, encrypt
from app.models import Project, ProjectToolConfig, Tool
from app.schemas.schemas import ProjectToolPatchIn, ToolPatchIn
from app.services import mcp_names, mcp_scan

router = APIRouter(prefix="/api/admin", tags=["tools"],
                   dependencies=[Depends(require_admin)])

_HTTP_RE = re.compile(r"^https?://", re.I)


def _tool_out(t: Tool) -> dict:
    return {"id": t.id, "slug": t.slug, "name": t.name, "kind": t.kind,
            "url": t.url, "enabled": t.enabled,
            "has_api_key": bool(t.api_key_enc),
            # The key the agent addresses this server by in a run - the string to
            # quote in project instructions.
            "mcp_server": mcp_names.tool_server_name(t)}


def _scan_or_409(name: str, url: str, key: str | None) -> None:
    findings, err = mcp_scan.audit_server(url, key)
    if findings:
        raise HTTPException(409, "The MCP server's tool definitions tripped the "
                                 "poisoning scan: " + "; ".join(findings[:3]))
    if err:
        raise HTTPException(409, f"Could not reach the MCP server: {err[:200]}")


@router.get("/tools")
async def list_tools(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Tool).order_by(Tool.created_at))).scalars().all()
    return [_tool_out(t) for t in rows]


@router.patch("/tools/{tool_id}")
async def patch_tool(tool_id: str, body: ToolPatchIn, db: AsyncSession = Depends(get_db)):
    t = await db.get(Tool, tool_id)
    if t is None:
        raise HTTPException(404, "Unknown tool")
    if body.url is not None:
        if not _HTTP_RE.match(body.url.strip()):
            raise HTTPException(422, "The MCP endpoint must be an http(s) URL")
        t.url = body.url.strip()
    if body.api_key is not None:
        t.api_key_enc = encrypt(body.api_key.strip()) if body.api_key.strip() else None
    if body.enabled is not None:
        if body.enabled:
            key = decrypt(t.api_key_enc) if t.api_key_enc else None
            await run_in_threadpool(_scan_or_409, t.name, t.url, key)
            t.tools_fingerprint = await run_in_threadpool(
                mcp_scan.fingerprint_tools, t.url, key)
        t.enabled = body.enabled
    await db.commit()
    return _tool_out(t)


@router.post("/tools/{tool_id}/verify")
async def verify_tool(tool_id: str, db: AsyncSession = Depends(get_db)):
    """Connection + poisoning check with the stored key; never persists state."""
    t = await db.get(Tool, tool_id)
    if t is None:
        raise HTTPException(404, "Unknown tool")
    key = decrypt(t.api_key_enc) if t.api_key_enc else None
    findings, err = await run_in_threadpool(mcp_scan.audit_server, t.url, key)
    if findings:
        return {"ok": False, "detail": "Poisoning scan findings: " + "; ".join(findings[:3])}
    if err:
        return {"ok": False, "detail": f"Could not reach the MCP server: {err[:200]}"}
    return {"ok": True, "detail": "Reachable - tool definitions are clean."}


@router.get("/projects/{project_id}/tools")
async def project_tools(project_id: str, db: AsyncSession = Depends(get_db)):
    if await db.get(Project, project_id) is None:
        raise HTTPException(404, "Unknown project")
    tools = (await db.execute(select(Tool).order_by(Tool.created_at))).scalars().all()
    ovs = {c.tool_id: c for c in (await db.execute(
        select(ProjectToolConfig).where(ProjectToolConfig.project_id == project_id)
    )).scalars().all()}
    out = []
    for t in tools:
        ov = ovs.get(t.id)
        out.append({**_tool_out(t),
                    "override_enabled": ov.enabled if ov else None,
                    "override_url": ov.url if ov else None,
                    "override_has_api_key": bool(ov.api_key_enc) if ov else False,
                    "effective_enabled": (ov.enabled if (ov and ov.enabled is not None)
                                          else t.enabled)})
    return out


@router.put("/projects/{project_id}/tools/{tool_id}")
async def put_project_tool(project_id: str, tool_id: str, body: ProjectToolPatchIn,
                           db: AsyncSession = Depends(get_db)):
    if await db.get(Project, project_id) is None:
        raise HTTPException(404, "Unknown project")
    t = await db.get(Tool, tool_id)
    if t is None:
        raise HTTPException(404, "Unknown tool")
    ov = (await db.execute(select(ProjectToolConfig).where(
        ProjectToolConfig.project_id == project_id,
        ProjectToolConfig.tool_id == tool_id))).scalar_one_or_none()
    if ov is None:
        ov = ProjectToolConfig(project_id=project_id, tool_id=tool_id)
        db.add(ov)
    if body.url is not None:
        u = body.url.strip()
        if u and not _HTTP_RE.match(u):
            raise HTTPException(422, "The MCP endpoint must be an http(s) URL")
        ov.url = u or None
    if body.api_key is not None:
        ov.api_key_enc = encrypt(body.api_key.strip()) if body.api_key.strip() else None
    if "enabled" in body.model_fields_set:
        if body.enabled:
            # never-trust-client: enabling FOR THIS PROJECT scans the effective
            # endpoint with the effective key (override -> global).
            url = ov.url or t.url
            key = (decrypt(ov.api_key_enc) if ov.api_key_enc
                   else decrypt(t.api_key_enc) if t.api_key_enc else None)
            await run_in_threadpool(_scan_or_409, t.name, url, key)
        ov.enabled = body.enabled
    await db.commit()
    return {"ok": True}
