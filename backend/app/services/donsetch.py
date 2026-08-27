"""§web research: which DonSeTch capabilities a build may call.

The sidecar serves one route per capability SET (`/{caps}/mcp`), so the enabled
set is carried in the URL rather than in a header the engine would have to
re-read. This module is the single place that turns a §Tools row's stored
`params` into that URL, and both callers use it: the admin API (which scans the
endpoint it is about to enable) and the dispatcher (which injects the endpoint
into a run's mcp.json). A capability the admin turned off is therefore absent
from `tools/list` for the whole build - not merely discouraged in a prompt.

`Tool.url` holds the sidecar BASE for this kind (`http://donsetch-mcp:3000`),
unlike the github/gitlab rows whose url is already a full MCP endpoint - the
path is derived, because it moves whenever the toggles do.
"""
SLUG = "donsetch"
KIND = "donsetch"

# Ordered: the URL is built from this, so a given set always yields one string
# (a stable endpoint keeps the sidecar's per-route reasoning predictable).
CAPABILITIES = ("search", "fetch", "crawl")

# What a freshly seeded row offers once an admin enables it. Search alone is the
# cheap, low-blast-radius half; fetch and crawl reach arbitrary pages and are the
# ones an operator may want to weigh, so they start off.
DEFAULT_CAPABILITIES = ("search",)

LABELS = {
    "search": "Web search",
    "fetch": "Page fetch",
    "crawl": "Site crawl",
}


def normalize(raw) -> list[str]:
    """Any client-supplied capability list → the canonical ordered subset."""
    if not raw:
        return []
    want = {str(c).strip().lower() for c in raw}
    return [c for c in CAPABILITIES if c in want]


def capabilities(tool) -> list[str]:
    """The capabilities stored on a §Tools row. A row whose params predate this
    feature (or were hand-cleared) reads as the seed default, never as 'all'."""
    params = tool.params or {}
    if "capabilities" not in params:
        return list(DEFAULT_CAPABILITIES)
    return normalize(params.get("capabilities"))


def endpoint(base_url: str, caps) -> str | None:
    """The sidecar route serving exactly `caps`, or None when none are enabled -
    a row with nothing turned on has no endpoint and must not reach a build."""
    caps = normalize(caps)
    if not caps:
        return None
    return f"{base_url.rstrip('/')}/{'+'.join(caps)}/mcp"


def tool_endpoint(tool, base_url: str | None = None) -> str | None:
    """The effective MCP endpoint for a §Tools row, honoring a URL override."""
    return endpoint(base_url or tool.url, capabilities(tool))
