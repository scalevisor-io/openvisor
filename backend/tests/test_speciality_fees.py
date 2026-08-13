"""§fees: the instance-level engagement-fee overrides. One resolver
(speciality.effective_base_fee) feeds the evaluation estimate, the public
/api/settings and the hub /api/hub/info, so the charged fee and every
advertised fee move together; the admin GET/PUT carries the rows.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.db import SyncSession
from app.core.deps import require_admin
from app.main import app
from app.models import AppSetting
from app.services import speciality
from app.services.pricing import load_static


@pytest.fixture(autouse=True)
def _clean_fee_settings():
    yield
    with SyncSession() as db:
        db.execute(delete(AppSetting).where(
            AppSetting.key == speciality.FEE_OVERRIDES_KEY))
        db.commit()


def _first_enabled_id() -> str:
    return next(s["id"] for s in load_static("specialities.json")["specialities"]
                if s.get("enabled"))


# --------------------------------------------------------------- the resolver

@pytest.mark.parametrize("raw,expected", [
    (12, 12.0), ("3.5", 3.5), (0, 0.0), (2.999, 3.0),
])
def test_clean_fee_ok(raw, expected):
    assert speciality.clean_fee(raw) == expected


@pytest.mark.parametrize("bad", [None, "", "abc", -1, float("nan"), float("inf")])
def test_clean_fee_rejects(bad):
    assert speciality.clean_fee(bad) is None


def test_effective_base_fee_precedence():
    spec = {"id": "track", "base_fee_credits": 10.0}
    assert speciality.effective_base_fee(spec, None) == 10.0
    assert speciality.effective_base_fee(spec, {}) == 10.0
    assert speciality.effective_base_fee(spec, {"other": 99}) == 10.0
    assert speciality.effective_base_fee(spec, {"track": 25}) == 25.0
    assert speciality.effective_base_fee(spec, {"track": 0}) == 0.0  # 0 disables
    # a garbled stored override falls back to the default, never crashes
    assert speciality.effective_base_fee(spec, {"track": "junk"}) == 10.0
    assert speciality.effective_base_fee(spec, {"track": -4}) == 10.0
    assert speciality.effective_base_fee({"id": "bare"}, {}) == 0.0


# --------------------------------------------------------------- admin surface

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(require_admin, None)
    asyncio.run(engine.dispose(close=False))


def test_admin_settings_carries_fee_rows(client):
    rows = client.get("/api/admin/settings").json()["speciality_fees"]
    assert rows, "enabled specialities must produce fee rows"
    row = rows[0]
    assert set(row) == {"id", "label", "default_fee_credits", "override_credits",
                        "effective_fee_credits"}
    assert row["override_credits"] is None
    assert row["effective_fee_credits"] == row["default_fee_credits"]


def _csrf(client) -> dict:
    return {"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrf_token"]}


def test_admin_override_set_clear_and_everyone_follows(client):
    sid = _first_enabled_id()
    body = client.put("/api/admin/settings", headers=_csrf(client),
                      json={"speciality_fee_overrides": {sid: 42.5}}).json()
    row = next(r for r in body["speciality_fees"] if r["id"] == sid)
    assert row["override_credits"] == 42.5
    assert row["effective_fee_credits"] == 42.5

    # the public settings page and the hub info advertise the SAME fee
    pub = client.get("/api/settings").json()["specialities"]
    assert next(s for s in pub if s["id"] == sid)["base_fee_credits"] == 42.5

    # clearing (null) falls back to the specialities.json default
    body = client.put("/api/admin/settings", headers=_csrf(client),
                      json={"speciality_fee_overrides": {sid: None}}).json()
    row = next(r for r in body["speciality_fees"] if r["id"] == sid)
    assert row["override_credits"] is None
    assert row["effective_fee_credits"] == row["default_fee_credits"]


def test_admin_override_validation(client):
    sid = _first_enabled_id()
    r = client.put("/api/admin/settings", headers=_csrf(client),
                   json={"speciality_fee_overrides": {"no-such-track": 5}})
    assert r.status_code == 422
    r = client.put("/api/admin/settings", headers=_csrf(client),
                   json={"speciality_fee_overrides": {sid: -3}})
    assert r.status_code == 422
    # neither rejected write may have stored anything
    with SyncSession() as db:
        assert db.get(AppSetting, speciality.FEE_OVERRIDES_KEY) is None


# --------------------------------------------------------------- the estimate

def test_evaluation_estimate_reads_the_override_through_the_resolver():
    """Source pin: the worker-side fee fold must go through the ONE resolver
    (an AppSetting read + effective_base_fee), never a direct
    base_fee_credits read - so the charged fee can't drift from the
    advertised one."""
    from app.agents import pipeline

    src = open(pipeline.__file__).read()
    assert "get_setting_sync(db, speciality_svc.FEE_OVERRIDES_KEY)" in src
    assert "speciality_svc.effective_base_fee(spec, overrides)" in src
    assert 'float(spec.get("base_fee_credits")' not in src
