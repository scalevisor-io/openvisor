"""Credit top-up checkout: the paying account's email and the amount floor.

Two things the customer sees on Stripe's page come from here: the Contact field
must arrive prefilled with the account that is paying (an empty field asks the
customer to retype what we already know, and a typo sends the receipt nowhere),
and an amount below the floor must be refused BEFORE a session exists - card
fees eat a tiny top-up, and Stripe's own minimum charge is lower than ours.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import settings
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import Organization, User
from app.services import stripe_svc


class _FakeSessions:
    """Stands in for stripe.checkout.Session - records what we asked Stripe for."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(url="https://checkout.stripe.test/c/pay/cs_test_1")


@pytest.fixture
def stripe_calls(monkeypatch):
    sessions = _FakeSessions()
    monkeypatch.setattr(stripe_svc, "stripe_lib", SimpleNamespace(
        api_key=None, checkout=SimpleNamespace(Session=sessions)))
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_billing_topup")
    return sessions.calls


@pytest.fixture
def customer():
    email = f"topup-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "topup-secret-123"
    with SyncSession() as db:
        org = Organization(name="Topup Org", credit_balance=0.0)
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        org_id = org.id
        db.commit()
    try:
        yield org_id, email, pwd
    finally:
        with SyncSession() as db:
            db.execute(delete(User).where(User.org_id == org_id))
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.commit()


@pytest.fixture
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def test_checkout_prefills_the_paying_account_email(stripe_calls):
    stripe_svc.create_topup_checkout("org-1", 20.0, "jean.dupont@example.com")
    assert stripe_calls[0]["customer_email"] == "jean.dupont@example.com"


def test_below_the_floor_never_reaches_stripe(stripe_calls):
    with pytest.raises(stripe_svc.TopupTooSmall):
        stripe_svc.create_topup_checkout("org-1", stripe_svc.MIN_TOPUP - 0.5,
                                         "jean.dupont@example.com")
    assert stripe_calls == []
    # The floor itself is payable - it is a minimum, not an exclusive bound.
    stripe_svc.create_topup_checkout("org-1", stripe_svc.MIN_TOPUP,
                                     "jean.dupont@example.com")
    assert len(stripe_calls) == 1


def test_topup_route_publishes_the_floor_and_bills_the_signed_in_email(
        client, customer, stripe_calls):
    """One login covers both halves (the login limiter is per test-client IP)."""
    _, email, pwd = customer
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    h = {"X-CSRF-Token": tok}

    # The SPA sizes its amount field from the balance payload.
    assert client.get("/api/billing/balance", headers=h).json()["min_topup"] == \
        stripe_svc.MIN_TOPUP

    r = client.post("/api/billing/topup", json={"amount": stripe_svc.MIN_TOPUP - 1},
                    headers=h)
    assert r.status_code == 400, r.text
    assert stripe_calls == []

    r = client.post("/api/billing/topup", json={"amount": 25}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"].startswith("https://checkout.stripe.test/")
    assert stripe_calls[0]["customer_email"] == email
