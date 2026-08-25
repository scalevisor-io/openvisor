"""§captcha: the self-hosted Altcha proof of work gates signup AND sign-in.

Sign-in is the new half: a credential-stuffing run has to pay the work on every
attempt, so the gate runs BEFORE the password check and a solved challenge is
worth exactly one attempt. The suite runs with the gate off (see conftest), so
every test here switches it back on.
"""
import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import CreditTransaction, Membership, Organization, User
from app.services import altcha
from tests.conftest import solve_altcha


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    # Heal the module-level async pools a prior HTTP test module left bound to
    # its now-closed loop (same dance as the other HTTP test modules).
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    """Turn the captcha back on, and clear the per-IP auth limiters.

    Every test here burns several signup and login attempts against one
    TestClient IP; without the reset the module starves itself on the limiter
    rather than on the thing it is testing.
    """
    from app.services import events
    monkeypatch.setattr(settings, "altcha_enabled", True)
    r = events.get_sync_redis()
    r.delete("rl:login:testclient")
    r.delete("rl:signup:testclient")


@pytest.fixture
def account():
    pwd = "captcha-secret-123"
    tag = uuid.uuid4().hex[:8]
    with SyncSession() as db:
        org = Organization(name="Captcha Org", credit_balance=0.0)
        db.add(org)
        db.flush()
        user = User(org_id=org.id, email=f"captcha-{tag}@example.com",
                    password_hash=hash_password(pwd), role="customer",
                    email_verified=True)
        db.add(user)
        db.commit()
        ids = {"email": user.email, "password": pwd, "org_id": org.id}
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(User).where(User.org_id == ids["org_id"]))
            db.execute(delete(Organization).where(Organization.id == ids["org_id"]))
            db.commit()


def _csrf(client) -> dict:
    return {"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrf_token"]}


def _forged(client) -> str:
    """A payload whose hash is self-consistent but whose signature is not ours."""
    challenge = client.get("/api/auth/altcha").json()
    return base64.b64encode(json.dumps({
        "algorithm": "SHA-256", "challenge": challenge["challenge"], "number": 1,
        "salt": challenge["salt"], "signature": "0" * 64}).encode()).decode()


def test_login_refused_without_a_solution(client, account):
    r = client.post("/api/auth/login", headers=_csrf(client),
                    json={"email": account["email"], "password": account["password"]})
    assert r.status_code == 400
    assert "captcha" in r.json()["detail"].lower()


def test_login_gate_runs_before_the_password_check(client, account):
    """A wrong password with no captcha must read as a captcha failure.

    That ordering is the point of the feature: if the password were checked
    first, a stuffing run would only pay the work on the guesses that land.
    """
    r = client.post("/api/auth/login", headers=_csrf(client),
                    json={"email": account["email"], "password": "wrong-password"})
    assert r.status_code == 400


def test_login_succeeds_with_a_solved_challenge(client, account):
    r = client.post("/api/auth/login", headers=_csrf(client), json={
        "email": account["email"], "password": account["password"],
        "altcha": solve_altcha(client)})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == account["email"]


def test_a_solved_challenge_is_worth_one_attempt(client, account):
    payload = solve_altcha(client)
    first = client.post("/api/auth/login", headers=_csrf(client), json={
        "email": account["email"], "password": account["password"], "altcha": payload})
    assert first.status_code == 200
    replay = client.post("/api/auth/login", headers=_csrf(client), json={
        "email": account["email"], "password": account["password"], "altcha": payload})
    assert replay.status_code == 400


def test_forged_signature_is_refused(client, account):
    r = client.post("/api/auth/login", headers=_csrf(client), json={
        "email": account["email"], "password": account["password"],
        "altcha": _forged(client)})
    assert r.status_code == 400


def test_signup_is_gated_too(client):
    body = {"email": f"captcha-signup-{uuid.uuid4().hex[:8]}@example.com",
            "password": "signup-secret-123", "accept_terms": True}
    assert client.post("/api/auth/signup", headers=_csrf(client),
                       json=body).status_code == 400

    body["altcha"] = solve_altcha(client)
    r = client.post("/api/auth/signup", headers=_csrf(client), json=body)
    assert r.status_code == 201, r.text
    with SyncSession() as db:
        user = db.execute(select(User).where(User.email == body["email"])).scalar_one()
        org_id = user.org_id
        db.execute(delete(Membership).where(Membership.user_id == user.id))
        db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == org_id))
        db.execute(delete(User).where(User.id == user.id))
        db.execute(delete(Organization).where(Organization.id == org_id))
        db.commit()


def test_disabled_gate_lets_a_bare_login_through(client, account, monkeypatch):
    """The knob is what a headless smoke test on a deployed stack switches off."""
    monkeypatch.setattr(settings, "altcha_enabled", False)
    r = client.post("/api/auth/login", headers=_csrf(client),
                    json={"email": account["email"], "password": account["password"]})
    assert r.status_code == 200


def test_public_settings_publishes_the_flag(client):
    assert client.get("/api/settings").json()["altcha_enabled"] is True


@pytest.mark.asyncio
async def test_verify_survives_a_redis_outage(monkeypatch, client):
    """Redis down must not lock everyone out: the work was still proven.

    Only the single-use check needs Redis, and it is the one check that can be
    dropped without accepting unproven work.
    """
    payload = solve_altcha(client)

    def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(altcha, "get_async_redis", _down)
    assert await altcha.verify_payload(payload) is True
