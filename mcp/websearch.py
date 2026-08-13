"""Web-search MCP sidecar (§KB `websearch` kind) - streamable HTTP, JSON-RPC.

One stateless FastAPI app serving `POST /{provider}/mcp`. The worker injects
`http://websearch-mcp:3000/<provider>/mcp` into a dev run's mcp.json for every
enabled+selected `websearch` KB row, with the row's envelope-decrypted API key
as `Authorization: Bearer` - exactly the header contract generic `mcp` rows
already use. The sidecar holds NO keys and NO state: each tools/call forwards
the caller's bearer to the provider, so the platform key never lives here, and
`tools/list` answers without auth (the worker's tool-poisoning vet fetches it
unauthenticated before every build).

Providers: `serper` - Google SERP via google.serper.dev (plain API calls; the
search stage only, no scrape/rerank pipeline); `staan` - the European search
index (staan.ai "Web for AI", the Qwant + Ecosia joint venture) - queries stay
under EU jurisdiction, matching the sovereign posture. Result text is capped so
one search can never blow the agent's context.

Privacy: queries and keys are never logged - provider errors surface to the
agent as MCP tool errors with the status code only.
"""
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="websearch-mcp")

PROTOCOL_VERSION = "2025-03-26"
SERPER_API_BASE = os.environ.get("SERPER_API_BASE", "https://google.serper.dev").rstrip("/")
STAAN_API_BASE = os.environ.get("STAAN_API_BASE", "https://api.staan.ai/v2").rstrip("/")
# Result market for Staan (e.g. fr-fr, en-us) - instance-level, not per-call.
STAAN_MARKET = os.environ.get("STAAN_MARKET", "fr-fr")
# Page-content chunks are off by default: they add ~8-10s latency and need a
# Staan tier that returns them; the basic snippet is already useful.
STAAN_EXTRA_SNIPPETS = os.environ.get("STAAN_EXTRA_SNIPPETS", "") == "true"
STAAN_MAX_SNIPPETS = max(1, min(int(os.environ.get("STAAN_MAX_SNIPPETS", "5")), 10))
STAAN_MIN_SCORE = os.environ.get("STAAN_MIN_SCORE", "0.2")
TIMEOUT_S = 20.0
DEFAULT_RESULTS = 5
MAX_RESULTS = 10
MAX_SNIPPET_CHARS = 500
MAX_TEXT_CHARS = 8000

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the public web for current, real-world information and return "
        "ranked results - title, URL, snippet and date when known. Use it for "
        "facts, library versions, vendor docs or news the connected knowledge "
        "sources can't answer; open a promising URL with the browser tool."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query, as you would type it into a search engine."},
            "num_results": {"type": "integer", "description": f"How many results to return (1-{MAX_RESULTS}, default {DEFAULT_RESULTS})."},
        },
        "required": ["query"],
    },
}


class ProviderError(Exception):
    """Agent-visible search failure - message must never contain the key or query."""


def _clip(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _serper_search(key: str, query: str, num: int) -> str:
    """One Serper /search call → the formatted result text handed to the agent."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.post(
            f"{SERPER_API_BASE}/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
        )
    if resp.status_code in (401, 403):
        raise ProviderError("the Serper API key was rejected (ask the platform admin to re-check it)")
    if resp.status_code != 200:
        raise ProviderError(f"Serper answered HTTP {resp.status_code}")
    data = resp.json()
    lines: list[str] = []
    box = data.get("answerBox") or {}
    if box.get("answer") or box.get("snippet"):
        lines.append(f"Answer box: {_clip(box.get('answer') or box.get('snippet'))}")
    kg = data.get("knowledgeGraph") or {}
    if kg.get("title") and kg.get("description"):
        lines.append(f"Knowledge graph: {kg['title']} - {_clip(kg['description'])}")
    for i, r in enumerate((data.get("organic") or [])[:num], 1):
        date = f" ({r['date']})" if r.get("date") else ""
        lines.append(f"{i}. {r.get('title', '(untitled)')}{date}\n   {r.get('link', '')}\n   {_clip(r.get('snippet', ''))}")
    if not lines:
        return "No results."
    return "\n".join(lines)[:MAX_TEXT_CHARS]


async def _staan_search(key: str, query: str, num: int) -> str:
    """One Staan Web-for-AI call → the formatted result text handed to the agent.
    NOTE: /v2/search/web is the Web-for-AI product - NOT /v2/answer, which is
    separately priced and 403s on keys without that entitlement."""
    params = {"q": query, "market": STAAN_MARKET}
    if STAAN_EXTRA_SNIPPETS:
        params.update(extra_snippets="true", max_snippets=str(STAAN_MAX_SNIPPETS),
                      min_score=STAAN_MIN_SCORE)
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(f"{STAAN_API_BASE}/search/web", params=params,
                                headers={"Authorization": f"Bearer {key}"})
    if resp.status_code in (401, 403):
        raise ProviderError("the Staan API key was rejected or lacks the Web-for-AI "
                            "entitlement (ask the platform admin to re-check it)")
    if resp.status_code != 200:
        raise ProviderError(f"Staan answered HTTP {resp.status_code}")
    results = ((resp.json().get("web") or {}).get("results") or [])[:num]
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        date = f" ({r['published_date']})" if r.get("published_date") else ""
        lines.append(f"{i}. {r.get('title', '(untitled)')}{date}\n   {r.get('url', '')}\n   {_clip(r.get('snippet', ''))}")
        for chunk in (r.get("extra_snippets") or [])[:STAAN_MAX_SNIPPETS]:
            if chunk.get("chunk"):
                lines.append(f"   > {_clip(chunk['chunk'])}")
    if not lines:
        return "No results."
    return "\n".join(lines)[:MAX_TEXT_CHARS]


PROVIDERS = {"serper": _serper_search, "staan": _staan_search}


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _rpc(id_, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _tool_text(id_, text: str, is_error: bool = False) -> JSONResponse:
    return _rpc(id_, {"content": [{"type": "text", "text": text}], "isError": is_error})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "providers": sorted(PROVIDERS)}


@app.post("/{provider}/mcp")
async def mcp(provider: str, request: Request):
    search = PROVIDERS.get(provider)
    if search is None:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32602, "message": f"unknown provider '{provider}'"}},
                            status_code=404)
    body = await request.json()
    method, id_ = body.get("method"), body.get("id")
    if method == "initialize":
        return _rpc(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"websearch-{provider}", "version": "1.0"},
        })
    if method == "notifications/initialized":
        return _rpc(id_, {})
    if method == "tools/list":
        return _rpc(id_, {"tools": [WEB_SEARCH_TOOL]})
    if method == "tools/call":
        params = body.get("params") or {}
        if (params.get("name")) != "web_search":
            return _tool_text(id_, f"unknown tool {params.get('name')!r}", is_error=True)
        args = params.get("arguments") or {}
        query = (args.get("query") or "").strip()
        if not query:
            return _tool_text(id_, "web_search needs a non-empty 'query'", is_error=True)
        try:
            num = max(1, min(int(args.get("num_results") or DEFAULT_RESULTS), MAX_RESULTS))
        except (TypeError, ValueError):
            num = DEFAULT_RESULTS
        key = _bearer(request)
        if not key:
            return _tool_text(id_, "no API key configured for this web-search source", is_error=True)
        try:
            return _tool_text(id_, await search(key, query, num))
        except ProviderError as exc:
            return _tool_text(id_, f"web search failed: {exc}", is_error=True)
        except httpx.HTTPError:
            return _tool_text(id_, "web search failed: the provider did not answer in time", is_error=True)
    return JSONResponse({"jsonrpc": "2.0", "id": id_,
                         "error": {"code": -32601, "message": f"method {method!r} not supported"}})
