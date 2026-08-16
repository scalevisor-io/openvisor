"""§After-shots: photograph a just-booted verify sandbox through the shared
browser-mcp service, inside the deployer's verify window (the sandbox is torn
down right after the boot check, so nobody later has a live app to shoot).

Deliberately dependency-free (urllib only - the deployer image carries no HTTP
client) and best-effort top to bottom: a screenshot must never fail or slow a
boot verdict beyond its own small budget. One isolated MCP session per capture,
closed on the way out, so the browser service never accumulates our state."""
import base64
import json
import logging
import os
import urllib.request

log = logging.getLogger("uvicorn.error")

BROWSER_MCP_URL = os.environ.get("BROWSER_MCP_URL", "http://browser-mcp:3000/mcp")
_STEP_TIMEOUT_S = 40  # navigate can legitimately take a while on a cold app


def _call(session: str | None, payload: dict) -> tuple[dict, str | None]:
    """One MCP POST. Returns (parsed result-or-{}, session id header)."""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    req = urllib.request.Request(BROWSER_MCP_URL, headers=headers,
                                 data=json.dumps(payload).encode())
    with urllib.request.urlopen(req, timeout=_STEP_TIMEOUT_S) as resp:
        sid = resp.headers.get("mcp-session-id")
        body = resp.read().decode(errors="replace")
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:]), sid
    return (json.loads(body) if body.strip() else {}), sid


def _tool(session: str, name: str, arguments: dict) -> dict:
    out, _ = _call(session, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})
    return out.get("result", {})


def capture(app_url: str, viewports: list) -> list[dict]:
    """[{width, height, png_b64}] for each viewport, or [] on any failure.

    The screenshot rides back base64 in the MCP tool result (playwright-mcp
    returns the PNG inline), so nothing depends on the browser pod's disk."""
    shots: list[dict] = []
    session = None
    try:
        _, session = _call(None, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "openvisor-after-shots", "version": "1"}}})
        if not session:
            raise RuntimeError("browser-mcp returned no session id")
        _call(session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        for width, height in viewports:
            _tool(session, "browser_resize", {"width": int(width), "height": int(height)})
            nav = _tool(session, "browser_navigate", {"url": app_url})
            if any("### Error" in (c.get("text") or "")
                   for c in nav.get("content", []) if c.get("type") == "text"):
                raise RuntimeError(f"navigate failed at {width}x{height}")
            shot = _tool(session, "browser_take_screenshot", {})
            png = next((c.get("data") for c in shot.get("content", [])
                        if c.get("type") == "image"), None)
            if not png:
                raise RuntimeError(f"no image content at {width}x{height}")
            # sanity: decodes and is bounded (a runaway page must not ship megabytes)
            if len(base64.b64decode(png)) > 5 * 1024 * 1024:
                raise RuntimeError(f"screenshot too large at {width}x{height}")
            shots.append({"width": int(width), "height": int(height), "png_b64": png})
        return shots
    except Exception as exc:  # noqa: BLE001 - screenshots never fail a verify
        log.warning("after-shots capture failed for %s: %s", app_url, exc)
        return []
    finally:
        if session:
            try:
                headers = {"mcp-session-id": session}
                req = urllib.request.Request(BROWSER_MCP_URL, headers=headers, method="DELETE")
                urllib.request.urlopen(req, timeout=10).close()
            except Exception:  # noqa: BLE001
                pass
