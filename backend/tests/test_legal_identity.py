"""§legal identity: the operating company printed in the landing's legal pages.

The Privacy policy and Terms of service are a STATIC build, so they read the
admin-set name/address from `GET /api/meta/legal` in the browser. That makes two
things load-bearing here: the endpoint answers unauthenticated and cross-origin
(the landing is served from another host than the API), and an unset field comes
back as "" - the landing reads that as "keep what I was built with" rather than
printing an empty company name into a legal document.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import AppSetting, Organization, User
from app.services import app_settings


@pytest.fixture(autouse=True)
def _clean_rows():
    def _drop():
        with SyncSession() as db:
            db.execute(delete(AppSetting).where(AppSetting.key.in_(
                [app_settings.LEGAL_NAME, app_settings.LEGAL_ADDRESS])))
            db.commit()
    _drop()
    yield
    _drop()


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
    email = f"legal-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "legal-secret-123"
    with SyncSession() as db:
        org = Organization(name="Legal Org")
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


def test_unset_identity_reads_as_empty_and_is_publicly_fetchable(client):
    client.cookies.clear()
    r = client.get("/api/meta/legal")
    assert r.status_code == 200, r.text
    assert r.json() == {"legal_name": "", "legal_address": ""}
    # The landing is a static site on another host: without this header the swap
    # never happens and the legal pages keep whatever was baked in at build time.
    assert r.headers["access-control-allow-origin"] == "*"


def test_admin_sets_the_identity_and_the_public_route_serves_it(client, admin):
    """One login covers the whole round trip (the login limiter is per client IP)."""
    email, pwd = admin
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    assert client.post("/api/auth/login", json={"email": email, "password": pwd},
                       headers={"X-CSRF-Token": tok}).status_code == 200
    h = {"X-CSRF-Token": tok}

    saved = client.put("/api/admin/settings", headers=h, json={
        "legal_name": "  Example Consulting Ltd  ",
        "legal_address": "12 Example Street\n75001 Paris\nFrance",
    })
    assert saved.status_code == 200, saved.text
    # The write answers with the stored settings, so the page renders what landed.
    assert saved.json()["legal_name"] == "Example Consulting Ltd"

    body = client.get("/api/meta/legal").json()
    assert body["legal_name"] == "Example Consulting Ltd"
    assert body["legal_address"] == "12 Example Street\n75001 Paris\nFrance"

    # Omitting a field leaves it alone; sending "" clears it back to the landing's
    # built-in value (the admin form's way of saying "use what was built in").
    client.put("/api/admin/settings", headers=h, json={"legal_name": ""})
    body = client.get("/api/meta/legal").json()
    assert body["legal_name"] == ""
    assert body["legal_address"] == "12 Example Street\n75001 Paris\nFrance"

    # Both fields are bounded - a legal page is not a text dump.
    too_long = client.put("/api/admin/settings", headers=h,
                          json={"legal_name": "x" * 201})
    assert too_long.status_code == 422

    # The admin page reads the same pair back with the rest of the settings.
    listed = client.get("/api/admin/settings", headers=h).json()
    assert listed["legal_name"] == ""
    assert listed["legal_address"].startswith("12 Example Street")


def test_customer_cannot_set_the_legal_identity(client):
    """The identity is instance-wide; only an admin writes it."""
    email = f"legal-cust-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "legal-secret-123"
    with SyncSession() as db:
        org = Organization(name="Legal Cust Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        org_id = org.id
        db.commit()
    try:
        client.cookies.clear()
        tok = client.get("/api/auth/csrf").json()["csrf_token"]
        assert client.post("/api/auth/login", json={"email": email, "password": pwd},
                           headers={"X-CSRF-Token": tok}).status_code == 200
        r = client.put("/api/admin/settings", headers={"X-CSRF-Token": tok},
                       json={"legal_name": "Not Mine SAS"})
        assert r.status_code == 403
        assert client.get("/api/meta/legal").json()["legal_name"] == ""
    finally:
        with SyncSession() as db:
            db.execute(delete(User).where(User.org_id == org_id))
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.commit()
