"""§KB websearch kind: server-side provider-key verification.

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

# Providers an admin can enable; seed.py creates one row per slug.
PROVIDERS = ("serper", "staan")


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
