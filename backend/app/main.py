from fastapi import Depends, FastAPI

from app.api import (
    account, admin, auth, billing, chat, chat_images, demo, files, hub, knowledge,
    knowledge_bases,
    tools, memory, meta, model_endpoints, org_memory, program_hooks, programs, projects,
    mcp_delegate,
    mcp_projects,
    mcp_tokens,
    tokens,
    usage,
)
from app.api import settings as settings_api
from app.core.config import settings
from app.core.deps import csrf_protect

app = FastAPI(title=f"{settings.brand_name} API", docs_url=None, redoc_url=None, openapi_url=None)

# CSRF applies to every /api route except the Stripe webhook (signature-verified)
_csrf = [Depends(csrf_protect)]

app.include_router(auth.router, dependencies=_csrf)
app.include_router(account.router, dependencies=_csrf)
app.include_router(meta.router)
app.include_router(settings_api.router)  # public brand/activity catalog, GET-only
app.include_router(projects.router, dependencies=_csrf)
app.include_router(chat.router, dependencies=_csrf)
app.include_router(chat_images.router, dependencies=_csrf)
app.include_router(chat.ws_router)
app.include_router(memory.router, dependencies=_csrf)
app.include_router(files.router, dependencies=_csrf)
app.include_router(org_memory.router, dependencies=_csrf)
app.include_router(programs.router, dependencies=_csrf)
app.include_router(programs.admin_router, dependencies=_csrf)
app.include_router(demo.router, dependencies=_csrf)
app.include_router(tokens.router, dependencies=_csrf)
app.include_router(mcp_tokens.router, dependencies=_csrf)
app.include_router(mcp_projects.router, dependencies=_csrf)  # §MCP projects: one-click create
app.include_router(usage.router, dependencies=_csrf)
# Token-authed (no session, no CSRF) - the MCP sidecar proxies here.
app.include_router(mcp_delegate.router)
app.include_router(mcp_projects.tool_router)  # MCP create_project tool
app.include_router(billing.quotes_router, dependencies=_csrf)
app.include_router(admin.router, dependencies=_csrf)
app.include_router(knowledge_bases.router, dependencies=_csrf)  # admin-gated CRUD
app.include_router(tools.router, dependencies=_csrf)  # §Tools admin CRUD
app.include_router(model_endpoints.router, dependencies=_csrf)  # admin-gated CRUD
app.include_router(knowledge.router)  # bearer-token auth, no cookies/CSRF
app.include_router(hub.router)  # hub-token auth, no cookies/CSRF
app.include_router(billing.router)  # webhook must stay CSRF-free
app.include_router(program_hooks.router)  # §28 inbound hooks: HMAC-authed, no cookies/CSRF


@app.get("/api/health")
async def health():
    return {"ok": True}
