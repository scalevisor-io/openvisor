"""Streamable-HTTP MCP scaffolding shared by every sidecar in this image.

Everything the dev agent calls is addressed by URL (`.openvisor/mcp.json` maps a
server key to an endpoint), so a sidecar here is an HTTP MCP server: one route,
JSON-RPC in, JSON-RPC out. The handshake is identical for all of them, and
getting it subtly wrong is the kind of bug that only shows up mid-build, so it
lives here once and every sidecar inherits it:

- `tools/list` answers WITHOUT auth. The worker's tool-poisoning vet fetches it
  unauthenticated before every build (`mcp_scan.audit_server`); a sidecar that
  401s there is dropped from the run.
- The caller's `Authorization: Bearer` is forwarded to the provider per request
  and never stored. The platform holds one envelope-encrypted key per §Tools row
  plus an optional per-project override, so a process that bound a key at start
  could only ever serve one of them.
- A provider failure is a TOOL error (`isError`), not a protocol error: the
  agent should read it and choose differently, not lose the server.

A new sidecar is a module with a `TOOLS` list and an async `dispatch`, plus one
`mount()` line in app.py - no compose, chart, NetworkPolicy or image-push change.
"""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

PROTOCOL_VERSION = "2025-03-26"


class ToolError(Exception):
    """An agent-visible failure. The message must never carry the API key."""


def clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() or None if auth.lower().startswith("bearer ") else None


def _rpc(id_, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _text(id_, text: str, is_error: bool = False) -> JSONResponse:
    return _rpc(id_, {"content": [{"type": "text", "text": text}], "isError": is_error})


def mcp_router(name: str, tools: list[dict], dispatch, max_chars: int) -> APIRouter:
    """One MCP server as a router serving `POST /mcp` under its mount prefix.

    `dispatch(tool, arguments, key)` is awaited for `tools/call` and returns the
    text handed to the agent; it raises ToolError for anything the agent should
    read. The result is clipped to `max_chars` HERE rather than in each sidecar,
    because an uncapped provider response is a context leak in every one of them.
    """
    router = APIRouter()
    by_name = {t["name"]: t for t in tools}

    @router.post("/mcp")
    async def mcp(request: Request):  # noqa: ANN202 - FastAPI route
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "request body is not JSON"}})
        method, id_ = body.get("method"), body.get("id")
        if method == "initialize":
            return _rpc(id_, {"protocolVersion": PROTOCOL_VERSION,
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": name, "version": "1.0"}})
        if (method or "").startswith("notifications/"):
            return _rpc(id_, {})
        if method == "tools/list":
            return _rpc(id_, {"tools": tools})
        if method != "tools/call":
            return JSONResponse({"jsonrpc": "2.0", "id": id_,
                                 "error": {"code": -32601, "message": f"method {method!r} not supported"}})
        params = body.get("params") or {}
        tool = params.get("name")
        if tool not in by_name:
            return _text(id_, f"unknown tool {tool!r}", is_error=True)
        try:
            out = await dispatch(tool, params.get("arguments") or {}, bearer(request))
        except ToolError as exc:
            return _text(id_, str(exc), is_error=True)
        except httpx.HTTPError:
            return _text(id_, "the provider did not answer in time", is_error=True)
        return _text(id_, out[:max_chars])

    return router
