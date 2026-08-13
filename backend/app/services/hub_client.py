"""Spoke -> hub MCP client (PROMPT hub link). A standalone spoke leaves
settings.hub_mcp_url empty and never calls this. When a hub is configured, the
Celery hub tasks use these helpers to register with, heartbeat to, and report
usage to a central Scalevisor Hub over JSON-RPC 2.0 (MCP tools/call),
authenticating with hub_spoke_token. Sync (Celery-side) with tight timeouts; any
transport/HTTP/JSON-RPC/tool error surfaces as HubError for the caller to log."""
import itertools
import json

import httpx

from app.core.config import settings

_ids = itertools.count(1)
_TIMEOUT = 10.0


class HubError(Exception):
    """A hub call failed (transport, HTTP status, JSON-RPC error, or isError result)."""


def _result_text(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content", [])
                     if c.get("type") == "text")


def _result_json(result: dict) -> dict:
    """Parse the tool's JSON payload out of the MCP text content (the hub, like
    the spoke MCP, serializes tool returns as json.dumps in a text block)."""
    text = _result_text(result)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HubError(f"hub tool returned non-JSON payload: {text[:200]}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _call_tool(name: str, arguments: dict) -> dict:
    if not settings.hub_mcp_url:
        raise HubError("hub_mcp_url is not configured")
    payload = {"jsonrpc": "2.0", "id": next(_ids), "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    headers = {"Authorization": f"Bearer {settings.hub_spoke_token}",
               "Content-Type": "application/json"}
    try:
        resp = httpx.post(settings.hub_mcp_url, headers=headers, json=payload,
                          timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise HubError(f"hub transport error: {exc}") from exc
    if resp.status_code >= 400:
        raise HubError(f"hub HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise HubError("hub returned a non-JSON response") from exc
    if data.get("error"):
        raise HubError(f"hub JSON-RPC error: {data['error']}")
    result = data.get("result") or {}
    if result.get("isError"):
        raise HubError(f"hub tool error: {_result_text(result)}")
    return result


def register_spoke(payload: dict) -> dict:
    """Announce this spoke to the hub (called once, then flagged in AppSetting)."""
    return _result_json(_call_tool("register_spoke", payload))


def heartbeat(payload: dict) -> dict:
    """Liveness + wallet snapshot ping."""
    return _result_json(_call_tool("heartbeat", payload))


def report_usage(events: list[dict]) -> dict:
    """Ship a batch of credit-transaction events. Returns the hub's parsed ack
    payload ({acked, cursor}); the caller must verify acked covers the batch."""
    return _result_json(_call_tool("report_usage", {"events": events}))


def report_project_events(events: list[dict]) -> dict:
    """Ship a batch of from-hub project outbox events (§pass-through P1).
    Returns the hub's parsed ack payload ({acked}); the caller must verify acked
    covers the batch before marking rows sent (at-least-once, hub dedups on id)."""
    return _result_json(_call_tool("report_project_events", {"events": events}))
