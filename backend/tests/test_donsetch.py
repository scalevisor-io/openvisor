"""§web research - the DonSeTch §Tools row: capability resolution, the URL the
capability set derives, the admin gate and what reaches a build's mcp.json.

The sidecar's own protocol surface is pinned in test_donsetch_sidecar.py. What
matters here is that the admin's toggles are load-bearing on the SERVER: the
endpoint injected into a run encodes exactly the enabled set, a row with nothing
enabled never reaches a build at all, and neither state can be talked into
existence by a client."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.main import app
from app.models import ProjectToolConfig, Tool
from app.seed import seed_tools
from app.services import donsetch, events
from app.workers import tasks


def _reset():
    with SyncSession() as db:
        db.execute(delete(ProjectToolConfig))
        row = db.execute(select(Tool).where(Tool.slug == donsetch.SLUG)).scalar_one()
        row.enabled = False
        row.params = {"capabilities": list(donsetch.DEFAULT_CAPABILITIES)}
        db.commit()


@pytest.fixture(autouse=True)
def seeded():
    # Reset on BOTH sides: this row is admin-editable, so a dev instance (or an
    # earlier test) can leave it toggled, and a seed-default assertion would then
    # read whatever was left behind rather than the seed.
    seed_tools()
    _reset()
    yield
    _reset()


def _row(db) -> Tool:
    return db.execute(select(Tool).where(Tool.slug == donsetch.SLUG)).scalar_one()


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    # the suite's earlier logins may have tripped the per-IP limiter
    r = events.get_sync_redis()
    for k in r.scan_iter("rl:*"):
        r.delete(k)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    """Logged-in admin; returns the CSRF headers every mutation needs."""
    from app.core.config import settings
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login",
                    json={"email": settings.admin_email, "password": settings.admin_password},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


# ── capability resolution ────────────────────────────────────────────────

def test_seeded_row_is_disabled_and_search_only():
    with SyncSession() as db:
        row = _row(db)
        assert row.enabled is False
        assert row.kind == donsetch.KIND
        # Search is the cheap half; fetch and crawl reach arbitrary pages and
        # are the operator's call, so they start off.
        assert donsetch.capabilities(row) == ["search"]


def test_capabilities_are_canonically_ordered():
    assert donsetch.normalize(["crawl", "search"]) == ["search", "crawl"]
    assert donsetch.normalize(["SEARCH", " fetch "]) == ["search", "fetch"]
    assert donsetch.normalize(["nonsense"]) == []


def test_row_without_params_reads_as_the_default_never_as_all():
    # A row predating this feature must not silently grant fetch and crawl.
    class _Bare:
        params = None
    assert donsetch.capabilities(_Bare()) == list(donsetch.DEFAULT_CAPABILITIES)


def test_endpoint_encodes_the_enabled_set():
    assert donsetch.endpoint("http://ds:3000", ["search", "fetch"]) == \
        "http://ds:3000/search+fetch/mcp"
    assert donsetch.endpoint("http://ds:3000/", ["crawl"]) == "http://ds:3000/crawl/mcp"
    # Nothing enabled = no endpoint at all, not a bare /mcp that serves everything.
    assert donsetch.endpoint("http://ds:3000", []) is None


# ── what reaches a build ─────────────────────────────────────────────────

def test_mcp_config_injects_only_the_enabled_capabilities(monkeypatch):
    monkeypatch.setattr(tasks.mcp_scan, "audit_server", lambda url, key: ([], None))
    with SyncSession() as db:
        row = _row(db)
        row.enabled = True
        row.params = {"capabilities": ["search", "crawl"]}
        db.commit()
        cfg, secrets = tasks._mcp_config(db)
        assert "/search+crawl/mcp" in cfg
        # Keyless: nothing to leak-scan, and no Authorization header.
        assert secrets == [] or all("donsetch" not in s for s in secrets)
        assert "Authorization" not in cfg.split('"donsetch"')[1][:200]


def test_mcp_config_skips_a_row_with_every_capability_off(monkeypatch):
    monkeypatch.setattr(tasks.mcp_scan, "audit_server", lambda url, key: ([], None))
    with SyncSession() as db:
        row = _row(db)
        row.enabled = True
        row.params = {"capabilities": []}
        db.commit()
        cfg, _ = tasks._mcp_config(db)
        assert "donsetch" not in cfg


def test_disabled_row_never_reaches_a_build(monkeypatch):
    monkeypatch.setattr(tasks.mcp_scan, "audit_server", lambda url, key: ([], None))
    with SyncSession() as db:
        cfg, _ = tasks._mcp_config(db)
        assert "donsetch" not in cfg


# ── the admin gate ───────────────────────────────────────────────────────

def test_admin_sees_capabilities_and_the_full_menu(client, admin):
    rows = client.get("/api/admin/tools").json()
    row = next(r for r in rows if r["slug"] == donsetch.SLUG)
    assert row["capabilities"] == ["search"]
    assert [c["slug"] for c in row["all_capabilities"]] == ["search", "fetch", "crawl"]
    # Keyless - the UI has no key to render.
    assert row["has_api_key"] is False


def test_patch_capabilities_round_trips(client, admin):
    with SyncSession() as db:
        tool_id = _row(db).id
    out = client.patch(f"/api/admin/tools/{tool_id}",
                       json={"capabilities": ["crawl", "search"]}, headers=admin).json()
    assert out["capabilities"] == ["search", "crawl"]


def test_enabling_with_no_capability_is_refused(client, admin):
    with SyncSession() as db:
        tool_id = _row(db).id
    r = client.patch(f"/api/admin/tools/{tool_id}",
                     json={"capabilities": [], "enabled": True}, headers=admin)
    assert r.status_code == 422


def test_clearing_the_last_capability_while_enabled_is_refused(client, admin, monkeypatch):
    monkeypatch.setattr("app.api.tools.mcp_scan.audit_server", lambda url, key: ([], None))
    monkeypatch.setattr("app.api.tools.mcp_scan.fingerprint_server", lambda url, key: "fp")
    with SyncSession() as db:
        tool_id = _row(db).id
    assert client.patch(f"/api/admin/tools/{tool_id}",
                        json={"enabled": True}, headers=admin).status_code == 200
    r = client.patch(f"/api/admin/tools/{tool_id}", json={"capabilities": []}, headers=admin)
    assert r.status_code == 422


def test_capabilities_are_rejected_on_other_kinds(client, admin):
    with SyncSession() as db:
        gh = db.execute(select(Tool).where(Tool.slug == "github")).scalar_one().id
    r = client.patch(f"/api/admin/tools/{gh}", json={"capabilities": ["search"]}, headers=admin)
    assert r.status_code == 422


def test_enable_scans_the_capability_route_not_the_base(client, admin, monkeypatch):
    seen = {}

    def _audit(url, key):
        seen["url"] = url
        return [], None

    monkeypatch.setattr("app.api.tools.mcp_scan.audit_server", _audit)
    monkeypatch.setattr("app.api.tools.mcp_scan.fingerprint_server", lambda url, key: "fp")
    with SyncSession() as db:
        tool_id = _row(db).id
    client.patch(f"/api/admin/tools/{tool_id}",
                 json={"capabilities": ["search", "fetch"], "enabled": True}, headers=admin)
    # The poisoning scan must see the tool list the run will actually get.
    assert seen["url"].endswith("/search+fetch/mcp")
