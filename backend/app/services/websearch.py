"""§Tools websearch kinds: the provider contract and server-side key verification.

A websearch row can only be enabled once its API key passes a live probe against
the provider (never trust the client - same discipline as the git-source
connection check). One tiny search per verification; the query is a fixed
constant so nothing user- or project-derived ever reaches the provider from
here. Keys never appear in errors or logs.
"""
import httpx

SERPER_API_BASE = "https://google.serper.dev"
STAAN_API_BASE = "https://api.staan.ai/v2"
_PROBE_TIMEOUT = 12.0

# Providers an admin can enable; seed.py creates one §Tools row per slug.
PROVIDERS = ("serper", "staan")

KIND = "websearch"

# Display names for the seeded rows, and the shelf copy each card carries.
PROVIDER_NAMES = {
    "serper": "Web search - Serper (Google)",
    "staan": "Web search - Staan (European index)",
}


def tool_slug(provider: str) -> str:
    """The §Tools slug for a provider.

    It keeps the `websearch_` prefix the dispatcher used to derive from the KB
    row's name, because `mcp_names.tool_server_name` slugs THIS string and that
    is the name a run addresses the server by - the one project instructions
    quote. Renaming it to a bare `serper` would silently break every instruction
    that says `websearch_serper`.
    """
    return f"websearch_{provider}"


def provider_of(tool) -> str | None:
    """The provider a §Tools row speaks for, or None when it is not one of ours."""
    if tool.kind != KIND:
        return None
    provider = (tool.params or {}).get("provider")
    return provider if provider in PROVIDERS else None


def endpoint(base_url: str, provider: str) -> str:
    """The websearch-mcp sidecar route serving one provider."""
    return f"{base_url.rstrip('/')}/{provider}/mcp"


def verify_key(provider: str, key: str) -> tuple[bool, str]:
    """(ok, error) - sync, called via run_in_threadpool from the API route."""
    if provider not in PROVIDERS:
        return False, f"unknown web-search provider '{provider}'"
    if not key:
        return False, "an API key is required"
    try:
        if provider == "serper":
            resp = httpx.post(f"{SERPER_API_BASE}/search",
                              headers={"X-API-KEY": key, "Content-Type": "application/json"},
                              json={"q": "ping", "num": 1}, timeout=_PROBE_TIMEOUT)
        else:  # staan - the Web-for-AI product, NOT /answer (separate entitlement)
            resp = httpx.get(f"{STAAN_API_BASE}/search/web",
                             params={"q": "ping", "market": "fr-fr"},
                             headers={"Authorization": f"Bearer {key}"},
                             timeout=_PROBE_TIMEOUT)
    except httpx.HTTPError as exc:
        return False, f"could not reach the provider: {type(exc).__name__}"
    if resp.status_code in (401, 403):
        return False, "the provider rejected this API key"
    if resp.status_code != 200:
        return False, f"the provider answered HTTP {resp.status_code}"
    return True, ""
