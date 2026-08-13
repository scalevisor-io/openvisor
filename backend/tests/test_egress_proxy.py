"""§egress filtering proxy (mcp/egress_proxy.py) - the enforcement point's pure
matching + safety logic. The proxy is a standalone image; compose.dev mounts its
source read-only at /app/mcp_src, so it loads by file path and skips where that
mount is absent (same discipline as test_mcp_scopes / test_websearch_sidecar).

Pinned here: FQDN + wildcard + IP/CIDR matching, and the hard refusal of
private/loopback/link-local targets (metadata SSRF / DNS-rebinding defence) that
holds regardless of an allowlist match.
"""
import importlib.util
import pathlib

import pytest

PROXY_SRC = pathlib.Path("/app/mcp_src/egress_proxy.py")


@pytest.fixture()
def px(monkeypatch):
    if not PROXY_SRC.exists():
        pytest.skip("egress proxy source not mounted at /app/mcp_src")
    spec = importlib.util.spec_from_file_location("egress_proxy_under_test", PROXY_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load(px, entries):
    import ipaddress
    px._FQDNS, px._NETS = [], []
    for e in entries:
        try:
            px._NETS.append(ipaddress.ip_network(e, strict=False))
        except ValueError:
            px._FQDNS.append(e)


def test_fqdn_exact_and_wildcard(px):
    _load(px, ["pypi.org", "*.githubusercontent.com"])
    assert px.host_matches_name("pypi.org")
    assert not px.host_matches_name("evilpypi.org")
    assert px.host_matches_name("raw.githubusercontent.com")
    assert px.host_matches_name("githubusercontent.com")       # *.x matches the apex too
    assert not px.host_matches_name("githubusercontent.com.evil.com")
    assert not px.host_matches_name("notgithubusercontent.com")


def test_cidr_and_ip(px):
    _load(px, ["10.0.0.0/8", "93.184.216.34"])
    assert px.ip_in_nets("10.1.2.3")
    assert px.ip_in_nets("93.184.216.34")
    assert not px.ip_in_nets("8.8.8.8")


def test_blocks_private_and_metadata(px):
    for ip in ("169.254.169.254", "10.0.0.5", "127.0.0.1", "192.168.1.1", "::1"):
        assert px.is_blocked_ip(ip) is True
    assert px.is_blocked_ip("93.184.216.34") is False


def test_decide_allows_public_allowlisted(px, monkeypatch):
    _load(px, ["example.com"])
    monkeypatch.setattr(px, "_resolve", lambda h: "93.184.216.34")
    allowed, reason, ip = px.decide("example.com")
    assert allowed and ip == "93.184.216.34"


def test_decide_denies_unlisted(px, monkeypatch):
    _load(px, ["example.com"])
    monkeypatch.setattr(px, "_resolve", lambda h: "8.8.8.8")
    allowed, reason, _ = px.decide("evil.com")
    assert not allowed and "not in allowlist" in reason


def test_decide_refuses_allowlisted_host_resolving_private(px, monkeypatch):
    # DNS-rebinding: the host is allowlisted but resolves to a private/metadata IP.
    _load(px, ["rebind.example"])
    monkeypatch.setattr(px, "_resolve", lambda h: "169.254.169.254")
    allowed, reason, _ = px.decide("rebind.example")
    assert not allowed and "private" in reason


def test_decide_literal_private_ip_target_refused(px):
    _load(px, ["10.0.0.0/8"])  # even an allowlisted CIDR can't reach a private literal
    allowed, reason, _ = px.decide("10.0.0.5")
    assert not allowed and "private" in reason
