"""§user blocking: an admin can lock a user out from /admin/users. Login is
refused with an explicit message (post-credential check), existing sessions die
on their next request, API tokens stop authenticating, and admin accounts can
never be blocked."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.db import SyncSession
from app.core.security import hash_password, new_api_token
from app.main import app
from app.models import ApiToken, Organization, User


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    # Heal the module-level async pools a prior HTTP test module left bound to
    # its now-closed loop (same dance as the other HTTP test modules).
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    # This module re-logs-in on every actor switch (one shared cookie jar) -
    # reset the shared per-IP login counter so neither side starves the other.
    events.get_sync_redis().delete("rl:login:testclient")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def world():
    """One org with a customer, plus a dedicated admin actor."""
    pwd = "blocking-secret-123"
    tag = uuid.uuid4().hex[:8]
    with SyncSession() as db:
        org = Organization(name="Blocking Org", credit_balance=0.0)
        db.add(org)
        db.flush()
        customer = User(org_id=org.id, email=f"block-target-{tag}@example.com",
                        password_hash=hash_password(pwd), role="customer",
                        email_verified=True)
        admin = User(org_id=org.id, email=f"block-admin-{tag}@example.com",
                     password_hash=hash_password(pwd), role="admin",
                     email_verified=True)
        db.add_all([customer, admin])
        db.commit()
        ids = {"pwd": pwd, "customer": customer.email, "customer_id": customer.id,
               "admin": admin.email, "admin_id": admin.id, "org_id": org.id}
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(ApiToken).where(
                ApiToken.user_id.in_([ids["customer_id"], ids["admin_id"]])))
            db.execute(delete(User).where(User.org_id == ids["org_id"]))
            db.execute(delete(Organization).where(Organization.id == ids["org_id"]))
            db.commit()


def _auth(client, email, pwd, expect=200):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == expect, r.text
    return {"X-CSRF-Token": tok}, r


def _set_blocked(user_id, value):
    with SyncSession() as db:
        db.get(User, user_id).blocked = value
        db.commit()


def test_blocked_login_gets_an_explicit_message(client, world):
    _set_blocked(world["customer_id"], True)
    try:
        _, r = _auth(client, world["customer"], world["pwd"], expect=403)
        assert "blocked" in r.json()["detail"].lower()
        # wrong password on a blocked account stays the generic 401 - the
        # blocked verdict must not leak to a credential probe
        tok = client.get("/api/auth/csrf").json()["csrf_token"]
        r = client.post("/api/auth/login",
                        json={"email": world["customer"], "password": "wrong-pw-1"},
                        headers={"X-CSRF-Token": tok})
        assert r.status_code == 401
    finally:
        _set_blocked(world["customer_id"], False)


def test_block_kills_the_existing_session(client, world):
    h, _ = _auth(client, world["customer"], world["pwd"])
    assert client.get("/api/auth/me", headers=h).status_code == 200
    _set_blocked(world["customer_id"], True)
    try:
        assert client.get("/api/auth/me", headers=h).status_code == 401
    finally:
        _set_blocked(world["customer_id"], False)


def test_block_kills_api_tokens(client, world):
    plaintext, token_hash = new_api_token()
    with SyncSession() as db:
        db.add(ApiToken(user_id=world["customer_id"], token_hash=token_hash,
                        name="t", scope="user"))
        db.commit()
    _set_blocked(world["customer_id"], True)
    try:
        r = client.post("/api/knowledge/answer", json={"query": "anything"},
                        headers={"Authorization": f"Bearer {plaintext}"})
        assert r.status_code == 403
        assert r.json()["detail"] == "Account blocked"
    finally:
        _set_blocked(world["customer_id"], False)


def test_admin_toggles_blocking_and_admins_are_untouchable(client, world):
    h, _ = _auth(client, world["admin"], world["pwd"])
    uid = world["customer_id"]

    r = client.patch(f"/api/admin/users/{uid}", json={"blocked": True}, headers=h)
    assert r.status_code == 200
    assert r.json() == {"id": uid, "blocked": True, "email_verified": True}
    listed = {u["id"]: u for u in client.get("/api/admin/users", headers=h).json()}
    assert listed[uid]["blocked"] is True

    # an admin target is refused, even by another admin
    r = client.patch(f"/api/admin/users/{world['admin_id']}",
                     json={"blocked": True}, headers=h)
    assert r.status_code == 403

    # an empty patch changes nothing
    r = client.patch(f"/api/admin/users/{uid}", json={}, headers=h)
    assert r.status_code == 200 and r.json()["blocked"] is True

    assert client.patch(f"/api/admin/users/{uuid.uuid4()}",
                        json={"blocked": True}, headers=h).status_code == 404


def test_admin_verifies_an_email_by_hand(client, world):
    """The manual stand-in for the verification link: an unverified customer can
    sign in but every require_verified route (project creation, MCP tokens)
    answers 403 email_not_verified until an admin flips the flag."""
    uid = world["customer_id"]
    with SyncSession() as db:
        db.get(User, uid).email_verified = False
        db.commit()
    hc, _ = _auth(client, world["customer"], world["pwd"])
    r = client.get("/api/mcp/tokens", headers=hc)
    assert r.status_code == 403 and r.json()["detail"] == "email_not_verified"

    h, _ = _auth(client, world["admin"], world["pwd"])
    r = client.patch(f"/api/admin/users/{uid}", json={"email_verified": True}, headers=h)
    assert r.status_code == 200
    assert r.json() == {"id": uid, "blocked": False, "email_verified": True}
    listed = {u["id"]: u for u in client.get("/api/admin/users", headers=h).json()}
    assert listed[uid]["email_verified"] is True

    # the verified customer passes the gate like one who clicked the link
    hc, _ = _auth(client, world["customer"], world["pwd"])
    assert client.get("/api/mcp/tokens", headers=hc).status_code == 200
