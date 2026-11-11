"""Admin management of the instance's MCP Tools (§Tools).

Tools ACT (GitHub/GitLab PR, issue and review operations over MCP); knowledge
bases inform. One global list of seeded rows (github, gitlab - disabled until
configured; the GitLab URL is editable for self-hosted instances,
https://<host>/api/v4/mcp) plus per-project overrides: tri-state enable, a URL
override (a customer's own GitLab instance) and a key override. Keys are
envelope-encrypted and NEVER returned (has_api_key only). Enabling a tool -
globally or per project - re-runs the §KB tool-poisoning scan server-side and
409s on findings (never trust the client). The `email` row carries SMTP
settings and a recipient allowlist instead of a key (services/email_tool): its
password is write-only like a key, enabling it proves the SMTP login, and it
takes no per-project URL or key."""
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import require_admin
from app.core.encryption import decrypt, encrypt
from app.models import Project, ProjectToolConfig, Tool, User
from app.schemas.schemas import ProjectToolPatchIn, ToolPatchIn
from app.services import brand, donsetch, email_tool, mcp_names, mcp_scan, websearch

router = APIRouter(prefix="/api/admin", tags=["tools"],
                   dependencies=[Depends(require_admin)])

_HTTP_RE = re.compile(r"^https?://", re.I)


def _tool_out(t: Tool) -> dict:
    out = {"id": t.id, "slug": t.slug, "name": t.name, "kind": t.kind,
           "url": t.url, "enabled": t.enabled,
           "has_api_key": bool(t.api_key_enc),
           # The key the agent addresses this server by in a run - the string to
           # quote in project instructions.
           "mcp_server": mcp_names.tool_server_name(t)}
    if t.kind == donsetch.KIND:
        out["capabilities"] = donsetch.capabilities(t)
        out["all_capabilities"] = [{"slug": c, "label": donsetch.LABELS[c]}
                                   for c in donsetch.CAPABILITIES]
    if t.kind == websearch.KIND:
        out["provider"] = websearch.provider_of(t)
    if t.kind == email_tool.KIND:
        out["email"] = email_tool.public_config(t)
    return out


async def _probe_email_or_409(t: Tool, refusal: str = "Can't enable email") -> None:
    """Never trust the client: an email row only turns on (or stays on through
    an SMTP change) once the stored settings actually log in."""
    ok, detail = await run_in_threadpool(email_tool.probe, t)
    if not ok:
        raise HTTPException(409, f"{refusal}: {detail}")


def _scan_url(t: Tool, url: str | None = None) -> str:
    """The endpoint to probe for a row: §web research serves one route per
    capability set, so the scan must hit the route the run will actually get."""
    if t.kind == donsetch.KIND:
        endpoint = donsetch.tool_endpoint(t, url)
        if endpoint is None:
            raise HTTPException(422, "Enable at least one web-research capability first")
        return endpoint
    return url or t.url


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
    smtp_changed = False
    if t.kind == email_tool.KIND and body.api_key is not None:
        raise HTTPException(422, "The email tool has no API key - set the SMTP password "
                                 "in its settings")
    if body.email is not None:
        if t.kind != email_tool.KIND:
            raise HTTPException(422, "This tool has no email settings")
        try:
            smtp_changed = email_tool.apply_config(t, body.email)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    if body.api_key is not None:
        t.api_key_enc = encrypt(body.api_key.strip()) if body.api_key.strip() else None
    if body.capabilities is not None:
        if t.kind != donsetch.KIND:
            raise HTTPException(422, "This tool has no configurable capabilities")
        caps = donsetch.normalize(body.capabilities)
        if not caps and (t.enabled if body.enabled is None else body.enabled):
            raise HTTPException(422, "Enable at least one web-research capability, "
                                     "or disable the tool")
        t.params = dict(t.params or {}) | {"capabilities": caps}
    if t.kind == websearch.KIND and not t.api_key_enc:
        # A keyless provider can't search; mirror the source rule the KB page had.
        t.enabled = False
    if t.kind == email_tool.KIND and body.enabled:
        await _probe_email_or_409(t)
    elif t.kind == email_tool.KIND and body.enabled is None and t.enabled and smtp_changed:
        await _probe_email_or_409(t, "SMTP settings not saved - the tool is enabled, "
                                     "so they must log in first")
    if body.enabled is not None:
        if body.enabled:
            key = decrypt(t.api_key_enc) if t.api_key_enc else None
            if t.kind == websearch.KIND:
                # Never trust the client: the stored key is re-probed against the
                # PROVIDER on every enable, on top of the sidecar poisoning scan.
                provider = websearch.provider_of(t)
                ok, detail = await run_in_threadpool(
                    websearch.verify_key, provider or "", key or "")
                if not ok:
                    raise HTTPException(409, f"Can't enable this web-search source: {detail}")
            url = _scan_url(t)
            await run_in_threadpool(_scan_or_409, t.name, url, key)
            t.tools_fingerprint = await run_in_threadpool(
                mcp_scan.fingerprint_server, url, key)
        t.enabled = body.enabled
    await db.commit()
    return _tool_out(t)


@router.post("/tools/{tool_id}/verify")
async def verify_tool(tool_id: str, db: AsyncSession = Depends(get_db)):
    """Connection + poisoning check with the stored key; never persists state.
    The email row also proves its SMTP login first."""
    t = await db.get(Tool, tool_id)
    if t is None:
        raise HTTPException(404, "Unknown tool")
    if t.kind == email_tool.KIND:
        ok, detail = await run_in_threadpool(email_tool.probe, t)
        if not ok:
            return {"ok": False, "detail": f"SMTP: {detail}"}
    key = decrypt(t.api_key_enc) if t.api_key_enc else None
    findings, err = await run_in_threadpool(mcp_scan.audit_server, _scan_url(t), key)
    if findings:
        return {"ok": False, "detail": "Poisoning scan findings: " + "; ".join(findings[:3])}
    if err:
        return {"ok": False, "detail": f"Could not reach the MCP server: {err[:200]}"}
    if t.kind == email_tool.KIND:
        return {"ok": True, "detail": "SMTP login works, and the email sidecar is "
                                      "reachable with clean tool definitions."}
    return {"ok": True, "detail": "Reachable - tool definitions are clean."}


@router.post("/tools/{tool_id}/test-email")
async def test_email(tool_id: str, admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_db)):
    """Send one real message with the stored SMTP settings to the calling
    admin's own address. A login proves the credentials, not that the relay
    accepts the From address - only a delivered message does."""
    t = await db.get(Tool, tool_id)
    if t is None:
        raise HTTPException(404, "Unknown tool")
    if t.kind != email_tool.KIND:
        raise HTTPException(422, "Only the email tool sends test messages")
    if not email_tool.is_configured(t):
        raise HTTPException(409, "Set the SMTP host and the From address first")
    to = email_tool.normalize_address(admin.email)
    if to is None:
        raise HTTPException(409, "Your account's address can't receive a test message")
    try:
        await run_in_threadpool(
            email_tool.send, t, [to], brand.subject("Email tool test"),
            "This is a test of the email tool's SMTP settings. When a build uses "
            "the tool, its messages arrive from this sender.")
    except email_tool.EmailToolError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": f"Test message sent to {to}."}


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
    if t.kind == email_tool.KIND and ((body.url or "").strip() or (body.api_key or "").strip()):
        # The endpoint is our own sidecar and the credential is minted per run:
        # neither has a per-project value that would mean anything.
        raise HTTPException(422, "The email tool has no per-project URL or key - "
                                 "only its enable state can be overridden")
    if body.url is not None:
        u = body.url.strip()
        if u and not _HTTP_RE.match(u):
            raise HTTPException(422, "The MCP endpoint must be an http(s) URL")
        ov.url = u or None
    if body.api_key is not None:
        ov.api_key_enc = encrypt(body.api_key.strip()) if body.api_key.strip() else None
    if "enabled" in body.model_fields_set:
        if body.enabled and t.kind == email_tool.KIND:
            await _probe_email_or_409(t)
        if body.enabled:
            # never-trust-client: enabling FOR THIS PROJECT scans the effective
            # endpoint with the effective key (override -> global).
            url = _scan_url(t, ov.url or t.url)
            key = (decrypt(ov.api_key_enc) if ov.api_key_enc
                   else decrypt(t.api_key_enc) if t.api_key_enc else None)
            await run_in_threadpool(_scan_or_409, t.name, url, key)
        ov.enabled = body.enabled
    await db.commit()
    return {"ok": True}
