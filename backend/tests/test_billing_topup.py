"""Credit top-up checkout: who Stripe bills, and the amount floor.

Two things must be true before a Checkout session exists. The paying account has
to arrive as a Stripe CUSTOMER carrying its email and billing profile - the
invoice is rendered from the customer, so an anonymous session produces a
document addressed to nobody. And an amount below the floor has to be refused
BEFORE anything is created: card fees eat a tiny top-up, and Stripe's own
minimum charge is lower than ours.
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
from app.models import CreditTransaction, Organization, User
from app.services import stripe_svc


class _FakeStripe:
    """Stands in for the stripe module - records what we asked Stripe for."""

    def __init__(self):
        self.sessions = []
        self.customers_created = []
        self.customers_modified = []

    def module(self):
        rec = self

        class _Customer:
            @staticmethod
            def create(**kwargs):
                rec.customers_created.append(kwargs)
                return SimpleNamespace(id="cus_test_1")

            @staticmethod
            def retrieve(customer_id):
                return SimpleNamespace(id=customer_id, deleted=False)

            @staticmethod
            def modify(customer_id, **kwargs):
                rec.customers_modified.append((customer_id, kwargs))
                return SimpleNamespace(id=customer_id)

        class _TaxId:
            @staticmethod
            def list(**kwargs):
                return SimpleNamespace(data=[])

        def create_session(**kwargs):
            rec.sessions.append(kwargs)
            return SimpleNamespace(url="https://checkout.stripe.test/c/pay/cs_test_1")

        return SimpleNamespace(
            api_key=None,
            error=SimpleNamespace(StripeError=Exception),
            checkout=SimpleNamespace(Session=SimpleNamespace(create=create_session)),
            Customer=_Customer,
            TaxId=_TaxId,
            tax=SimpleNamespace(Registration=SimpleNamespace(
                list=lambda **k: SimpleNamespace(data=[]))),
        )


@pytest.fixture
def stripe(monkeypatch):
    rec = _FakeStripe()
    monkeypatch.setattr(stripe_svc, "stripe_lib", rec.module())
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_billing_topup")
    monkeypatch.setattr(stripe_svc, "_account_tax_id_cache", [])
    monkeypatch.setattr(stripe_svc, "_tax_registration_cache", None)
    return rec


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
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == org_id))
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


def test_the_session_bills_a_customer_not_an_anonymous_email(stripe):
    stripe_svc.create_topup_checkout("org-1", "cus_test_1", 20.0, "ref-1")
    assert stripe.sessions[0]["customer"] == "cus_test_1"


def test_below_the_floor_never_reaches_stripe(stripe):
    with pytest.raises(stripe_svc.TopupTooSmall):
        stripe_svc.create_topup_checkout("org-1", "cus_test_1",
                                         stripe_svc.MIN_TOPUP - 0.5, "ref-1")
    assert stripe.sessions == []
    # The floor itself is payable - it is a minimum, not an exclusive bound.
    stripe_svc.create_topup_checkout("org-1", "cus_test_1", stripe_svc.MIN_TOPUP, "ref-1")
    assert len(stripe.sessions) == 1


def test_topup_route_publishes_the_floor_and_bills_the_signed_in_account(
        client, customer, stripe):
    """One login covers both halves (the login limiter is per test-client IP)."""
    org_id, email, pwd = customer
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
    assert stripe.sessions == []

    r = client.post("/api/billing/topup", json={"amount": 25}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"].startswith("https://checkout.stripe.test/")
    # The customer is created once and carries the signed-in email, so Stripe's
    # Contact field arrives prefilled and the receipt reaches the right inbox.
    assert stripe.customers_created[0]["email"] == email
    assert stripe.customers_created[0]["metadata"] == {"org_id": org_id}
    # ...and the profile is pushed across BEFORE the session is created.
    assert stripe.customers_modified[0][1]["email"] == email
    assert stripe.sessions[0]["customer"] == "cus_test_1"

    # The id survives on the org, so the next top-up reuses the same customer
    # and the invoice history stays in one place.
    with SyncSession() as db:
        assert db.get(Organization, org_id).stripe_customer_id == "cus_test_1"
