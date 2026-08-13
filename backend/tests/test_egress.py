"""§egress: the dev-sandbox egress allowlist service + admin surface.

Covers entry validation (FQDN / wildcard / IP / CIDR), the effective-list merge
(admin list + the run's own hosts, so enabling lockdown can't sever LLM/git), the
default-off posture, and the admin GET/PUT contract (enable, save list, 422 on a
bad entry).
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.db import SyncSession
from app.core.deps import require_admin
from app.main import app
from app.models import AppSetting
from app.services import egress


@pytest.fixture(autouse=True)
def _clean_egress_settings():
    yield
    with SyncSession() as db:
        db.execute(delete(AppSetting).where(
            AppSetting.key.in_([egress.ENABLED_KEY, egress.ALLOWLIST_KEY])))
        db.commit()


# --------------------------------------------------------------- entry validation

@pytest.mark.parametrize("raw,expected", [
    ("PyPI.org", "pypi.org"),
    ("*.githubusercontent.com", "*.githubusercontent.com"),
    ("https://files.pythonhosted.org/packages", "files.pythonhosted.org"),
    ("93.184.216.34", "93.184.216.34/32"),
    ("10.0.0.0/8", "10.0.0.0/8"),
    ("2001:db8::/32", "2001:db8::/32"),
])
def test_normalize_entry_ok(raw, expected):
    assert egress.normalize_entry(raw) == expected


@pytest.mark.parametrize("bad", ["", "not a host", "http://", "exa mple.com", "*.", "foo_bar"])
def test_normalize_entry_rejects(bad):
    with pytest.raises(ValueError):
        egress.normalize_entry(bad)


def test_normalize_list_dedupes_preserving_order():
    assert egress.normalize_list(["pypi.org", "PyPI.org", "npmjs.org"]) == ["pypi.org", "npmjs.org"]


# ------------------------------------------------------------- effective allowlist

def test_default_off_and_default_list():
    with SyncSession() as db:
        assert egress.is_enabled(db) is False
        assert egress.get_allowlist(db) == egress.DEFAULT_ALLOWLIST


def test_effective_allowlist_auto_adds_run_hosts():
    with SyncSession() as db:
        eff = egress.effective_allowlist(
            db, llm_base_url="https://api.mistral.ai/v1",
            remote_url="https://gitlab.example.com/acme/app.git")
        assert "api.mistral.ai" in eff
        assert "gitlab.example.com" in eff
        # admin defaults still present, no duplicates
        assert "pypi.org" in eff and len(eff) == len(set(eff))


def test_effective_allowlist_uses_saved_list_when_present():
    with SyncSession() as db:
        db.add(AppSetting(key=egress.ALLOWLIST_KEY, value=["internal.example.com"]))
        db.commit()
    with SyncSession() as db:
        eff = egress.effective_allowlist(db, llm_base_url="https://api.mistral.ai/v1")
        assert "internal.example.com" in eff
        assert "pypi.org" not in eff  # the saved list REPLACES the default
        assert "api.mistral.ai" in eff  # run host still merged


def test_run_required_hosts_skips_junk():
    assert egress.run_required_hosts(llm_base_url="", remote_url="git@host:only-ssh") == \
        egress.run_required_hosts(llm_base_url="", remote_url="")  # ssh scp-form → no https host


# ------------------------------------------------------------------- HTTP surface

@pytest.fixture(scope="module")
def client():
    # Module-scoped, entered as a context manager so every request shares one loop.
    # Heal a pool a prior HTTP module may have left bound to its closed loop, and
    # dispose again on the way OUT so the NEXT module inherits a clean engine (the
    # connections are bound to this module's loop) - the convention every HTTP
    # module here follows (see test_git_identity / test_knowledge_bases).
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c
    asyncio.run(engine.dispose(close=False))


@pytest.fixture()
def admin_headers(client):
    """Authenticate WITHOUT the /login endpoint: this module spends zero of the
    shared 20/15min login-limiter budget (the suite already runs near that
    ceiling). require_admin is a router dependency, so overriding it satisfies
    admin auth; CSRF only needs the cookie GET /auth/csrf sets to equal the
    X-CSRF-Token header."""
    app.dependency_overrides[require_admin] = lambda: None
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    try:
        yield {"X-CSRF-Token": tok}
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_update_requires_admin(client):
    # No login needed (and none spent): with a valid CSRF token but no session,
    # require_admin's get_current_user rejects the unauthenticated caller (401).
    client.cookies.clear()
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    assert client.put("/api/admin/settings", headers={"X-CSRF-Token": tok},
                      json={"egress_lockdown_enabled": True}).status_code in (401, 403)


def test_admin_get_and_update(client, admin_headers):
    h = admin_headers

    # default posture: off, default list, K8s-scoped
    s = client.get("/api/admin/settings", headers=h).json()
    assert s["egress_lockdown_enabled"] is False
    assert "pypi.org" in s["egress_allowlist"]
    assert s["egress_enforced_on"] == "kubernetes"

    # save a normalized list + enable
    r = client.put("/api/admin/settings", headers=h, json={
        "egress_allowlist": ["Example.com", "*.internal.example", "10.0.0.0/8"],
        "egress_lockdown_enabled": True,
    }).json()
    assert r["egress_lockdown_enabled"] is True
    assert r["egress_allowlist"] == ["example.com", "*.internal.example", "10.0.0.0/8"]

    # a bad entry is a 422 and nothing is persisted from that call
    bad = client.put("/api/admin/settings", headers=h,
                     json={"egress_allowlist": ["ok.com", "not a host"]})
    assert bad.status_code == 422 and "not a host" in bad.json()["detail"]
    assert client.get("/api/admin/settings", headers=h).json()["egress_allowlist"] == \
        ["example.com", "*.internal.example", "10.0.0.0/8"]
