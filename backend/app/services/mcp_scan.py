"""Static tool-poisoning defence for admin-added MCP servers (§KB, `mcp` kind).

A malicious or compromised MCP server can embed instructions in its TOOL METADATA
(tool names, descriptions, parameter descriptions) that the coding agent ingests at
connect time - indirect prompt injection that fires before any tool is even called.
This is "MCP tool poisoning" (arXiv:2603.22489 / arXiv:2603.21642); it has been
demonstrated reading `~/.ssh` keys through a hidden tool parameter against a
production client. Most MCP clients do NO static validation of tool definitions, so
the OpenHands runner will not filter an admin-added server for us - we must.

`_mcp_config` wires admin-added servers straight into the dev run, so before doing
so it calls `audit_server`: fetch the server's `tools/list` and statically vet it.
A server whose metadata trips the scanner is DROPPED from that build (never handed
to the agent). Best-effort: an unreachable server (fetch error) is NOT dropped - a
transient outage must not silently disable a legitimate KB - only a server that
answers with dangerous tool metadata is. The scan/fingerprint helpers are pure and
offline; only `fetch_tools` touches the network.
"""
import hashlib
import json
import logging
import re

import httpx

log = logging.getLogger(__name__)

# Phrases with no legitimate place in a tool description - the signature of embedded
# instructions / tool poisoning. Case-insensitive over name + description + each
# parameter's name and description.
_POISON_PATTERNS = [
    r"ignore\s+(all|any|previous|prior|the)",
    r"disregard\s+(the|all|any|previous|prior)",
    r"system\s+prompt",
    r"do\s+not\s+(tell|mention|inform|reveal|report|disclose).{0,24}(user|human|them)",
    r"without\s+(telling|informing|notifying|alerting).{0,24}user",
    r"</?(system|instructions?|important|secret)>",
    r"exfiltrat",
    r"\.ssh\b|id_rsa|id_ed25519|private\s+key|\.env\b|/etc/passwd|\$HOME|~/\.",
    r"send\s+(it|them|the|this|contents?|data|files?|output).{0,30}(to|http|https|url|server|endpoint)",
    r"\b(curl|wget|nc)\b|base64\s+(-d|--decode|encode)",
    r"read\s+(the\s+)?(file|contents?|/etc/|~/)",
    r"before\s+(using|calling|invoking|running)\s+(this|the|any)\s+tool",
    r"sidenote|hidden\s+(instruction|parameter|field)",
]
_COMPILED = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in _POISON_PATTERNS]

# Code-point ranges used to smuggle INVISIBLE instructions into otherwise-innocent
# tool text: Unicode tag chars, zero-width spaces/joiners + LRM/RLM, bidi overrides,
# word joiner / invisible operators, and the BOM. Built from hex so the source file
# carries no literal invisible characters.
_INVISIBLE_RANGES = [
    (0xE0000, 0xE007F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x2064), (0xFEFF, 0xFEFF),
]
_INVISIBLE_RE = re.compile(
    "[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _INVISIBLE_RANGES) + "]")


def _tool_text(tool: dict) -> str:
    """Concatenate the human-readable metadata the agent actually ingests: tool name,
    description, and every parameter's name + description."""
    parts = [str(tool.get("name", "")), str(tool.get("description", ""))]
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict):
        for pname, pdef in props.items():
            parts.append(str(pname))
            if isinstance(pdef, dict):
                parts.append(str(pdef.get("description", "")))
    return "\n".join(parts)


def scan_tool_defs(tools: list) -> list[str]:
    """Return human-readable findings for tool definitions that look poisoned; an
    empty list means clean. Pure/offline."""
    findings: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name", "?"))
        text = _tool_text(tool)
        if _INVISIBLE_RE.search(text):
            findings.append(f"{name}: hidden/invisible unicode in tool metadata")
        for pat in _COMPILED:
            m = pat.search(text)
            if m:
                findings.append(f"{name}: suspicious phrase '{m.group(0)[:40]}'")
                break  # one phrase finding per tool is enough to drop it
    return findings


def fingerprint_tools(tools: list) -> str:
    """Stable sha256 over every tool's normalized (name + description + params) text,
    so a later 'rug pull' redefinition of a once-clean server is detectable by an
    equality check. Pure/offline."""
    norm = sorted(_tool_text(t) for t in (tools or []) if isinstance(t, dict))
    return hashlib.sha256(" ".join(norm).encode("utf-8")).hexdigest()


def _parse_jsonrpc(resp: httpx.Response) -> dict:
    """MCP streamable HTTP answers either JSON or an SSE event stream; handle both."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    continue
        return {}
    return resp.json()


def fetch_tools(url: str, api_key: str | None = None, timeout: float = 6.0) -> list:
    """Best-effort MCP `tools/list` over streamable-HTTP JSON-RPC (initialize ->
    notifications/initialized -> tools/list). Returns the tool list (possibly []).
    Raises on transport/protocol failure so the caller can tell a reachable-but-
    poisoned server from an unreachable one."""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with httpx.Client(timeout=timeout) as c:
        init = c.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "openvisor-mcp-scan", "version": "1"}}})
        init.raise_for_status()
        h2 = dict(headers)
        session = init.headers.get("mcp-session-id")
        if session:
            h2["mcp-session-id"] = session
        try:  # best-effort init notification; some servers require it before tools/list
            c.post(url, headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        resp = c.post(url, headers=h2, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp.raise_for_status()
        data = _parse_jsonrpc(resp)
    return (data.get("result") or {}).get("tools") or []


def audit_server(url: str, api_key: str | None = None) -> tuple[list[str], str | None]:
    """Fetch a server's tools and scan them. Returns (findings, error):
      - (non-empty, None): reachable and poisoned -> caller DROPS the server;
      - ([], None): reachable and clean -> caller keeps it;
      - ([], "<err>"): unreachable/protocol error -> caller keeps it (transient).
    Never raises."""
    try:
        tools = fetch_tools(url, api_key)
    except Exception as exc:
        return [], str(exc)
    return scan_tool_defs(tools), None
