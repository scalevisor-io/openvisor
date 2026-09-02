"""§consultant identity: who the practice is, admin-editable and stored as TWO
fields.

The reason for two rather than one string is `test_a_multiword_surname...`: no
heuristic can split "Ada Lovelace King" correctly, and the first name alone is
what the chat and the emails say. `services.brand` is the single resolver -
stored pair first, CONSULTANT_NAME second, per field - and every prompt, email
and API payload goes through it, so what is pinned here is the resolution order,
the per-field fallback, and that a save is visible immediately instead of after
the cache TTL.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import settings
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import AppSetting, Organization, User
from app.services import app_settings, brand

KEYS = (app_settings.CONSULTANT_FIRST_NAME, app_settings.CONSULTANT_LAST_NAME)


def _clear() -> None:
    with SyncSession() as db:
        db.execute(delete(AppSetting).where(AppSetting.key.in_(KEYS)))
        db.commit()
    brand.reset_cache()


def _store(first: str, last: str) -> None:
    with SyncSession() as db:
        app_settings.set_setting_sync(db, app_settings.CONSULTANT_FIRST_NAME, first)
        app_settings.set_setting_sync(db, app_settings.CONSULTANT_LAST_NAME, last)
        db.commit()
    brand.reset_cache()


@pytest.fixture(autouse=True)
def _clean_rows():
    _clear()
    yield
    _clear()


@pytest.fixture
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin():
    email = f"consultant-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "consultant-secret-123"
    with SyncSession() as db:
        org = Organization(name="Consultant Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="admin", email_verified=True))
        org_id = org.id
        db.commit()
    try:
        yield email, pwd
    finally:
        with SyncSession() as db:
            db.execute(delete(User).where(User.org_id == org_id))
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.commit()


# ---- the resolver ----

def test_falls_back_to_the_env_name_when_unset():
    assert brand.consultant_name() == settings.consultant_name
    assert brand.consultant_first_name() == settings.consultant_name.split()[0]


def test_stored_pair_wins():
    _store("Ada", "Lovelace")
    assert brand.consultant_parts() == ("Ada", "Lovelace")
    assert brand.consultant_name() == "Ada Lovelace"
    assert brand.consultant_first_name() == "Ada"


def test_a_multiword_surname_survives_the_round_trip():
    # The whole reason for two fields: splitting on the first space gives the
    # wrong surname here, and no heuristic can know better.
    _store("Ada", "Lovelace King")
    assert brand.consultant_parts()[1] == "Lovelace King"


def test_each_field_falls_back_on_its_own():
    # An admin who fills in only the first name keeps the env surname.
    _store("Ada", "")
    env_last = " ".join(settings.consultant_name.split()[1:])
    assert brand.consultant_parts() == ("Ada", env_last)


def test_a_consultant_with_no_surname_has_no_trailing_space(monkeypatch):
    monkeypatch.setattr(settings, "consultant_name", "Prince")
    brand.reset_cache()
    assert brand.consultant_name() == "Prince"


def test_placeholders_render_the_stored_name():
    _store("Ada", "Lovelace")
    assert brand.render("{{CONSULTANT_FIRST_NAME}} of {{CONSULTANT_NAME}}") == "Ada of Ada Lovelace"


def test_an_unreachable_database_degrades_to_the_env_name(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("database is down")
    monkeypatch.setattr(app_settings, "get_consultant_identity_sync", boom)
    brand.reset_cache()
    assert brand.consultant_name() == settings.consultant_name


# ---- the API surface ----

def test_public_settings_report_the_stored_name(client):
    _store("Ada", "Lovelace")
    data = client.get("/api/settings").json()
    assert data["consultant_name"] == "Ada Lovelace"
    assert data["consultant_first_name"] == "Ada"


def test_meta_consultant_is_public_and_cors_open(client):
    _store("Ada", "Lovelace")
    client.cookies.clear()
    r = client.get("/api/meta/consultant")
    assert r.status_code == 200
    # The landing is a static build on another host; without this the swap never
    # happens and the page keeps the name it was built with.
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.json() == {"first_name": "Ada", "last_name": "Lovelace",
                        "full_name": "Ada Lovelace"}


def test_admin_round_trip(client, admin):
    """One login covers the whole round trip (the login limiter is per client IP)."""
    email, pwd = admin
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    assert client.post("/api/auth/login", json={"email": email, "password": pwd},
                       headers={"X-CSRF-Token": tok}).status_code == 200
    h = {"X-CSRF-Token": tok}

    saved = client.put("/api/admin/settings", headers=h, json={
        "consultant_first_name": "  Ada  ", "consultant_last_name": "Lovelace King"})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    # Read back through the resolver in the SAME request: the admin who just
    # saved must not be shown the old name for the next five minutes.
    assert body["consultant_first_name"] == "Ada"
    assert body["consultant_last_name"] == "Lovelace King"
    assert body["consultant_name_effective"] == "Ada Lovelace King"
    assert client.get("/api/meta/consultant").json()["full_name"] == "Ada Lovelace King"

    # Omitting a field leaves it alone.
    body = client.put("/api/admin/settings", headers=h,
                      json={"consultant_first_name": "Grace"}).json()
    assert body["consultant_first_name"] == "Grace"
    assert body["consultant_last_name"] == "Lovelace King"

    # "" clears the override back to the env identity.
    body = client.put("/api/admin/settings", headers=h,
                      json={"consultant_first_name": "", "consultant_last_name": ""}).json()
    assert body["consultant_first_name"] == ""
    assert body["consultant_name_effective"] == settings.consultant_name

    # Both fields are bounded - a name is not a text dump.
    assert client.put("/api/admin/settings", headers=h,
                      json={"consultant_first_name": "x" * 101}).status_code == 422
