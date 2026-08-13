"""§egress: the instance-level dev-sandbox egress allowlist (agentic-security
audit measure 3, re-tightened with hostname support).

When the admin enables lockdown, a dev run's pod is network-locked so its ONLY
internet path is a per-run filtering proxy (see mcp/egress_proxy.py) that permits
just the allowlisted hosts. This is ENFORCED ON KUBERNETES ONLY (the deployer's
K8s path); compose dev-runs keep their existing open egress, exactly like every
other sandbox NetworkPolicy in this repo. The admin UI/docs say so - the toggle
is not a silent no-op on compose, it is documented as K8s-scoped.

This module is the single source of truth for: the two AppSetting keys, entry
validation (FQDN / wildcard FQDN / IP / CIDR), the sensible default list, and the
effective per-run list (admin list + the run's own required hosts, always merged
so enabling lockdown can never sever the LLM/git path - the 2026-07-12 regression).
"""
import ipaddress
import re
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.services import app_settings

ENABLED_KEY = "egress_lockdown_enabled"
ALLOWLIST_KEY = "egress_allowlist"

# Prefilled when the admin first opens the setting; a good starting point for a
# typical polyglot build (package registries + the common git hosts). The admin
# edits from here; the worker ALWAYS adds the run's own hosts on top (below).
DEFAULT_ALLOWLIST = [
    "pypi.org", "files.pythonhosted.org",                       # pip
    "registry.npmjs.org",                                        # npm
    "*.crates.io", "static.crates.io",                          # cargo
    "proxy.golang.org", "sum.golang.org",                      # go modules
    "rubygems.org",                                              # gem
    "github.com", "codeload.github.com", "*.githubusercontent.com",
    "gitlab.com",
    "registry-1.docker.io", "auth.docker.io", "*.docker.io",   # docker hub
    "deb.debian.org", "security.debian.org",                   # apt (debian)
]

_FQDN_RE = re.compile(r"^(?:\*\.)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$")


def normalize_entry(raw: str) -> str:
    """Validate + canonicalize one allowlist entry. Accepts a bare FQDN, a
    single-label wildcard FQDN (`*.example.com`), an IPv4/IPv6 address, or a CIDR.
    Lowercased; a full URL is reduced to its host. Raises ValueError on anything
    else - the caller turns that into a 422 naming the offending entry."""
    entry = (raw or "").strip().lower()
    if not entry:
        raise ValueError("empty entry")
    if "://" in entry:  # tolerate a pasted URL - keep the host[:port] only
        entry = urlsplit(entry).hostname or ""
    entry = entry.rstrip("/")
    if not entry:
        raise ValueError("empty entry")
    # IP or CIDR?
    try:
        return str(ipaddress.ip_network(entry, strict=False))
    except ValueError:
        pass
    if _FQDN_RE.match(entry):
        return entry
    raise ValueError(f"'{raw}' is not a valid FQDN, wildcard, IP or CIDR")


def normalize_list(entries: list[str]) -> list[str]:
    """Validate every entry (raises ValueError on the first bad one), de-dupe
    preserving order."""
    out: list[str] = []
    for e in entries:
        norm = normalize_entry(e)
        if norm not in out:
            out.append(norm)
    return out


def is_enabled(db: Session) -> bool:
    return bool(app_settings.get_setting_sync(db, ENABLED_KEY, False))


def get_allowlist(db: Session) -> list[str]:
    """The admin's stored list, or the default starter list if never saved."""
    stored = app_settings.get_setting_sync(db, ALLOWLIST_KEY, None)
    if stored is None:
        return list(DEFAULT_ALLOWLIST)
    return list(stored)


def _host_of(value: str) -> str | None:
    """Host of a URL or bare host:port; None if unusable."""
    value = (value or "").strip()
    if not value:
        return None
    if "://" in value:
        return urlsplit(value).hostname
    # bare host or host:port (not a URL) - split a trailing :port only
    return value.rsplit(":", 1)[0] if value.count(":") == 1 and value.rsplit(":", 1)[1].isdigit() else value


def run_required_hosts(*, llm_base_url: str = "", remote_url: str = "") -> list[str]:
    """Hosts a dev run always needs reachable, added on top of the admin list so
    enabling lockdown can't break the build: the LLM endpoint and the git remote
    (when it is an https remote - an ssh remote rides the NetworkPolicy Tailscale
    rule, not the proxy). In-cluster tools are reached directly (NO_PROXY), not
    through the proxy, so they are NOT listed here."""
    hosts: list[str] = []
    for raw in (llm_base_url, remote_url):
        host = _host_of(raw)
        if not host:
            continue
        try:
            hosts.append(normalize_entry(host))
        except ValueError:
            continue
    return hosts


def effective_allowlist(db: Session, *, llm_base_url: str = "",
                        remote_url: str = "") -> list[str]:
    """The admin list merged with the run's own required hosts (deduped)."""
    merged = list(get_allowlist(db))
    for host in run_required_hosts(llm_base_url=llm_base_url, remote_url=remote_url):
        if host not in merged:
            merged.append(host)
    return merged
