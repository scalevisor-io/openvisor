"""DonSeTch web-research MCP bridge (§web research) - streamable HTTP over a stdio engine.

DonSeTch speaks MCP on **stdio only**, but every MCP server this platform injects
into a dev run is addressed by URL. This bridge is the adapter: one stateless
FastAPI app serving `POST /{caps}/mcp`, each request proxied to a `donsetch mcp`
subprocess over newline-delimited JSON-RPC.

`caps` is the `+`-joined set of capabilities the admin left enabled on the Tools
row (`search`, `fetch`, `crawl`), so the SAME container serves a search-only
instance and a full-research instance without a restart. The capability set is
enforced twice - `tools/list` hides what is off, and `tools/call` refuses it -
because a filtered list is a courtesy to the model, not a gate.

Isolation: one engine process per MCP session, exactly like the browser sidecar's
`--isolated` profile. A dev run's cookies, page memory and S-handles are born on
`initialize` and die when the session is reaped, so one project's browsing state
can never surface in another's build. Sessionless callers (the tool-poisoning
scan, which fetches `tools/list` once before every build) get a short-lived
process of their own and never touch a run's engine.

DonSeTch itself is AGPL-3.0 and runs UNMODIFIED as a separate executable behind a
process boundary - see README.md in this directory.
"""
import asyncio
import contextlib
import json
import os
import time
import uuid

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

router = APIRouter()

PROTOCOL_VERSION = "2025-03-26"
DONSETCH_BIN = os.environ.get("DONSETCH_BIN", "donsetch")
# A dev run holds its engine between tool calls; this reaps the ones whose run
# died without closing the session (the sandbox is killed, not shut down).
SESSION_IDLE_S = float(os.environ.get("DONSETCH_SESSION_IDLE_S", "900"))
# Hard ceiling on one tool call. crawl is the slow one (many pages); the caller's
# own deadline_ms should normally fire first.
CALL_TIMEOUT_S = float(os.environ.get("DONSETCH_CALL_TIMEOUT_S", "180"))
INIT_TIMEOUT_S = float(os.environ.get("DONSETCH_INIT_TIMEOUT_S", "30"))
MAX_SESSIONS = int(os.environ.get("DONSETCH_MAX_SESSIONS", "24"))

# Tool name -> the capability slug that gates it. A tool absent from this map is
# unknown to us and stays hidden: a future DonSeTch release that adds a fourth
# tool must be reviewed here before any dev run can call it.
TOOL_CAPS = {"web_search": "search", "web_fetch": "fetch", "web_crawl": "crawl"}
ALL_CAPS = ("search", "fetch", "crawl")


def parse_caps(raw: str) -> list[str]:
    """`search+fetch` -> ['search', 'fetch']; `all` -> every capability."""
    if raw.strip().lower() == "all":
        return list(ALL_CAPS)
    want = {p.strip().lower() for p in raw.replace(",", "+").split("+") if p.strip()}
    return [c for c in ALL_CAPS if c in want]


class Engine:
    """One `donsetch mcp` subprocess, spoken to one request at a time."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.touched = time.monotonic()
        self._next_id = 10_000

    async def start(self) -> None:
        env = dict(os.environ)
        # Keep the engine's own updater off in a container: the image pins the
        # audited binary, and a self-update would swap it underneath us.
        env["DONSETCH_NO_UPDATE_CHECK"] = "1"
        self.proc = await asyncio.create_subprocess_exec(
            DONSETCH_BIN, "mcp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def request(self, payload: dict, timeout: float) -> dict:
        """Send one JSON-RPC request, return its response object."""
        if not self.alive:
            await self.start()
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.touched = time.monotonic()
        # Renumber onto our own id space: the engine's replies are matched by id,
        # and two clients must never collide on one.
        self._next_id += 1
        rpc_id = self._next_id
        sent = dict(payload)
        sent["id"] = rpc_id
        self.proc.stdin.write((json.dumps(sent) + "\n").encode())
        await self.proc.stdin.drain()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the engine did not answer in time")
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
            if not line:
                raise RuntimeError("the engine closed its output")
            try:
                msg = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue  # engine chatter, not a JSON-RPC frame
            if msg.get("id") == rpc_id:
                return msg

    async def notify(self, payload: dict) -> None:
        if not self.alive:
            return
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()

    async def close(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            self.proc.terminate()
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()


SESSIONS: dict[str, Engine] = {}


async def _reap() -> None:
    while True:
        await asyncio.sleep(60)
        cutoff = time.monotonic() - SESSION_IDLE_S
        for sid, eng in [(s, e) for s, e in SESSIONS.items() if e.touched < cutoff or not e.alive]:
            SESSIONS.pop(sid, None)
            await eng.close()


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    reaper = asyncio.create_task(_reap())
    try:
        yield
    finally:
        reaper.cancel()
        for eng in list(SESSIONS.values()):
            await eng.close()
        SESSIONS.clear()


app = FastAPI(title="donsetch-mcp", lifespan=_lifespan)


def _rpc(id_, result: dict, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result}, headers=headers or {})


def _err(id_, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}},
                        status_code=status)


def _tool_text(id_, text: str, is_error: bool = False) -> JSONResponse:
    return _rpc(id_, {"content": [{"type": "text", "text": text}], "isError": is_error})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "capabilities": list(ALL_CAPS), "sessions": len(SESSIONS)}


@router.post("/{caps}/mcp")
async def mcp(caps: str, request: Request):
    allowed = parse_caps(caps)
    if not allowed:
        return _err(None, -32602, f"no known capability in {caps!r}", status=404)
    try:
        body = await request.json()
    except ValueError:
        return _err(None, -32700, "request body is not JSON")
    method, id_ = body.get("method"), body.get("id")
    sid = request.headers.get("mcp-session-id")

    if method == "initialize":
        if len(SESSIONS) >= MAX_SESSIONS:
            return _err(id_, -32000, "too many concurrent web-research sessions")
        eng = Engine()
        try:
            async with eng.lock:
                resp = await eng.request(body, INIT_TIMEOUT_S)
        except Exception as exc:
            await eng.close()
            return _err(id_, -32000, f"could not start the web-research engine: {type(exc).__name__}")
        sid = uuid.uuid4().hex
        SESSIONS[sid] = eng
        result = resp.get("result") or {}
        # Answer in OUR protocol/server identity: the run addresses this bridge,
        # not the engine behind it.
        result.setdefault("protocolVersion", PROTOCOL_VERSION)
        result["serverInfo"] = {"name": "donsetch", "version": "1.0",
                                "capabilities_enabled": allowed}
        return _rpc(id_, result, headers={"Mcp-Session-Id": sid})

    if method == "notifications/initialized" or (method or "").startswith("notifications/"):
        eng = SESSIONS.get(sid or "")
        if eng:
            with contextlib.suppress(Exception):
                await eng.notify(body)
        return _rpc(id_, {})

    if method not in ("tools/list", "tools/call"):
        return _err(id_, -32601, f"method {method!r} not supported")

    if method == "tools/call":
        name = ((body.get("params") or {}).get("name")) or ""
        cap = TOOL_CAPS.get(name)
        if cap is None:
            return _tool_text(id_, f"unknown tool {name!r}", is_error=True)
        if cap not in allowed:
            return _tool_text(
                id_, f"the {name} capability is disabled for this instance", is_error=True)

    eng = SESSIONS.get(sid or "")
    ephemeral = eng is None
    if ephemeral:
        # No session: a one-shot caller (the tool-poisoning scan). Give it its own
        # engine and take it down again - never a run's.
        eng = Engine()
    try:
        async with eng.lock:
            resp = await eng.request(body, CALL_TIMEOUT_S)
    except TimeoutError:
        return _tool_text(id_, "the web-research engine did not answer in time", is_error=True)
    except Exception as exc:
        return _err(id_, -32000, f"web-research engine failure: {type(exc).__name__}")
    finally:
        if ephemeral:
            await eng.close()

    if method == "tools/list":
        result = resp.get("result") or {}
        tools = [t for t in (result.get("tools") or [])
                 if TOOL_CAPS.get(t.get("name")) in allowed]
        result["tools"] = tools
        return _rpc(id_, result)
    return JSONResponse({k: v for k, v in resp.items() if k != "id"} | {"jsonrpc": "2.0", "id": id_})


app.include_router(router)
