"""The billing profile: what an invoice is addressed to, and what taxes it.

The failure this pins is a quiet one. The profile was collected on the account
page from day one and never sent anywhere, so a paying organization received a
Stripe receipt with no legal name, no address, no VAT number and no tax line -
a document their accountant cannot file. Nothing errored, which is why it
survived: an invoice addressed to nobody looks exactly like one nobody has
asked for yet.

Every assertion here is about a value CROSSING the boundary into Stripe, or
about the one thing the customer cannot see for themselves: an address that is
partially filled in is withheld from Stripe entirely rather than sent, because a
partial one resolves to the wrong tax rate instead of to an error.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import CreditTransaction, Organization, User
from app.services import countries, stripe_svc


# --- the country table ------------------------------------------------------

@pytest.mark.parametrize("country,typed,expected", [
    # An EU VAT number is written with or without its prefix, and Greece files
    # under EL rather than GR - the exception that turns a regex into a wrong
    # rejection.
    ("FR", "FR12345678901", ("eu_vat", "FR12345678901")),
    ("FR", "12345678901", ("eu_vat", "FR12345678901")),
    ("FR", "fr 123 456 789 01", ("eu_vat", "FR12345678901")),
    ("GR", "123456789", ("eu_vat", "EL123456789")),
    ("DE", "DE811234567", ("eu_vat", "DE811234567")),
    ("GB", "123456789", ("gb_vat", "GB123456789")),
    ("GB", "GB123456789", ("gb_vat", "GB123456789")),
    ("CH", "CHE-123.456.789 MWST", ("ch_vat", "CHE-123.456.789 MWST")),
    ("CH", "123456789", ("ch_vat", "CHE-123.456.789 MWST")),
    ("NO", "123456789MVA", ("no_vat", "123456789MVA")),
    ("US", "123456789", ("us_ein", "12-3456789")),
    ("US", "12-3456789", ("us_ein", "12-3456789")),
    ("CA", "123456789RT0001", ("ca_gst_hst", "123456789RT0001")),
    ("CA", "1234567890TQ0001", ("ca_qst", "1234567890TQ0001")),
    ("CA", "123456789", ("ca_bn", "123456789")),
])
def test_a_tax_number_is_normalised_to_what_stripe_accepts(country, typed, expected):
    assert countries.tax_id_for(country, typed) == expected


@pytest.mark.parametrize("country,typed", [
    ("FR", "not-a-vat-number-at-all"),
    ("US", "12345"),           # too short for an EIN
    ("CA", "123456789RZ0001"),  # a payroll account, not a GST/HST one
    ("GB", "GB12345"),
    ("JP", "1234567890123"),   # a country we do not bill in
    ("FR", ""),
])
def test_an_unrecognisable_tax_number_is_withheld_not_guessed(country, typed):
    """Stripe validates the format and rejects the call, and that call is in the
    middle of a payment: a wrong guess does not produce a wrong invoice, it takes
    the whole top-up down."""
    assert countries.tax_id_for(country, typed) is None


def test_a_subdivision_is_required_only_where_the_rate_depends_on_one():
    eu = SimpleNamespace(id="o1", address_line1="1 rue de Rivoli", city="Paris",
                         postal_code="75001", country="FR", province=None)
    assert countries.missing_address_fields(eu) == []

    us = SimpleNamespace(id="o2", address_line1="1 Market St", city="San Francisco",
                         postal_code="94105", country="US", province=None)
    # Not a formality: without the state this resolves to a federal rate and
    # misses the other half of the bill.
    assert countries.missing_address_fields(us) == ["state"]


def test_countries_are_served_so_the_form_and_the_validator_cannot_disagree():
    served = {c["code"] for c in countries.out()}
    assert served == set(countries.SUPPORTED)
    france = next(c for c in countries.out() if c["code"] == "FR")
    assert france["subdivisions"] == []      # VAT is national; nothing to pick
    assert france["tax_id_label"] == "VAT number"
    canada = next(c for c in countries.out() if c["code"] == "CA")
    assert {s["code"] for s in canada["subdivisions"]} >= {"ON", "QC"}


# --- what actually reaches Stripe -------------------------------------------

class _Recorder:
    """Stands in for the stripe module: records every call, answers plausibly."""

    def __init__(self):
        self.sessions = []
        self.customers = []
        self.tax_ids = []
        self.deleted_tax_ids = []
        self.existing_tax_ids = []

    # checkout.Session.create
    def create_session(self, **kwargs):
        self.sessions.append(kwargs)
        return SimpleNamespace(url="https://checkout.stripe.test/c/pay/cs_test_1")

    def as_module(self):
        rec = self

        class _TaxId:
            @staticmethod
            def list(**kwargs):
                if (kwargs.get("owner") or {}).get("type") == "self":
                    return SimpleNamespace(data=[SimpleNamespace(id="txi_seller")])
                return SimpleNamespace(data=list(rec.existing_tax_ids))

            @staticmethod
            def create(**kwargs):
                rec.tax_ids.append(kwargs)
                return SimpleNamespace(id="txi_new")

            @staticmethod
            def delete(tax_id):
                rec.deleted_tax_ids.append(tax_id)

        class _Customer:
            @staticmethod
            def modify(customer_id, **kwargs):
                rec.customers.append((customer_id, kwargs))
                return SimpleNamespace(id=customer_id)

            @staticmethod
            def retrieve(customer_id):
                return SimpleNamespace(id=customer_id, deleted=False)

            @staticmethod
            def create(**kwargs):
                return SimpleNamespace(id="cus_created")

        return SimpleNamespace(
            api_key=None,
            error=SimpleNamespace(StripeError=Exception),
            checkout=SimpleNamespace(Session=SimpleNamespace(create=rec.create_session)),
            Customer=_Customer,
            TaxId=_TaxId,
            tax=SimpleNamespace(Registration=SimpleNamespace(
                list=lambda **k: SimpleNamespace(data=[SimpleNamespace(country="FR")]))),
        )


@pytest.fixture
def stripe(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(stripe_svc, "stripe_lib", rec.as_module())
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_billing_profile")
    # Cached for the life of a process, so a test run must not inherit another
    # test's answer.
    monkeypatch.setattr(stripe_svc, "_account_tax_id_cache", [])
    monkeypatch.setattr(stripe_svc, "_tax_registration_cache", None)
    return rec


def _org(**over):
    base = dict(id="org-1", type="organization", name="Dupont SARL",
                company_name="Dupont SARL", vat_id=None,
                address_line1="1 rue de Rivoli", address_line2=None,
                city="Paris", postal_code="75001", country="FR", province=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_the_invoice_name_follows_the_account_type(stripe):
    """An organization that switches back to individual keeps its stored company
    details on purpose, so `company_name or name` addressed a personal invoice
    to a company the account no longer says it is."""
    assert stripe_svc.billing_name(_org()) == "Dupont SARL"
    assert stripe_svc.billing_name(
        _org(type="individual", name="Jean Dupont")) == "Jean Dupont"

    stripe_svc.sync_customer("cus_1", _org(type="individual", name="Jean Dupont"),
                             "jean@example.com")
    assert stripe.customers[0][1]["name"] == "Jean Dupont"


def test_the_legal_name_and_address_reach_the_stripe_customer(stripe):
    """The invoice renders from the CUSTOMER, not from the session. A profile
    that lives only in Postgres produces an invoice addressed to nobody."""
    stripe_svc.sync_customer("cus_1", _org(), "jean.dupont@example.com")
    customer_id, payload = stripe.customers[0]
    assert customer_id == "cus_1"
    assert payload["name"] == "Dupont SARL"
    assert payload["address"] == {"line1": "1 rue de Rivoli", "line2": None,
                                  "city": "Paris", "postal_code": "75001",
                                  "country": "FR"}
    # VAT is national in the EU: a subdivision sent to France is at best ignored
    # and at worst a rejected address.
    assert "state" not in payload["address"]


def test_a_subdivision_is_sent_only_where_it_bills(stripe):
    stripe_svc.sync_customer("cus_1", _org(country="CA", province="QC",
                                           postal_code="H2Y 1C6", city="Montreal"),
                             "jean@example.com")
    _, payload = stripe.customers[0]
    assert payload["address"]["state"] == "QC"


def test_an_incomplete_address_is_withheld_whole_never_sent_partial(stripe):
    """A US address with no state resolves to a federal rate, not to an error,
    so half the bill would silently go uncollected."""
    stripe_svc.sync_customer("cus_1", _org(country="US", province=None,
                                           city="San Francisco", postal_code="94105"),
                             "jean@example.com")
    _, payload = stripe.customers[0]
    assert "address" not in payload
    assert payload["email"] == "jean@example.com"


def test_the_customers_vat_number_is_set_on_the_stripe_customer(stripe):
    stripe_svc.sync_customer("cus_1", _org(vat_id="fr 123 456 789 01"),
                             "jean@example.com")
    assert stripe.tax_ids == [{"type": "eu_vat", "value": "FR12345678901",
                               "owner": {"type": "customer", "customer": "cus_1"}}]


def test_a_stale_vat_number_is_replaced_not_stacked(stripe):
    """Stripe has no update for a tax id, and Checkout skips collection entirely
    once a customer holds one, so a stale number would be pinned forever."""
    stripe.existing_tax_ids = [SimpleNamespace(id="txi_old", value="FR99999999999")]
    stripe_svc.sync_customer("cus_1", _org(vat_id="FR12345678901"), "jean@example.com")
    assert stripe.deleted_tax_ids == ["txi_old"]
    assert stripe.tax_ids[0]["value"] == "FR12345678901"


def test_an_unchanged_vat_number_costs_no_write(stripe):
    stripe.existing_tax_ids = [SimpleNamespace(id="txi_old", value="FR12345678901")]
    stripe_svc.sync_customer("cus_1", _org(vat_id="FR12345678901"), "jean@example.com")
    assert stripe.tax_ids == []
    assert stripe.deleted_tax_ids == []


def test_a_personal_account_does_not_carry_a_company_vat_number(stripe):
    """A round-trip through organization keeps the stored number on purpose, and
    in the EU a customer VAT number moves the invoice to reverse charge - so
    sending a leftover one would issue a personal invoice with no tax at all."""
    stripe_svc.sync_customer("cus_1", _org(type="individual", name="Jean Dupont",
                                           vat_id="FR12345678901"),
                             "jean@example.com")
    assert stripe.tax_ids == []


def test_an_unrecognisable_vat_number_never_reaches_stripe(stripe):
    stripe_svc.sync_customer("cus_1", _org(vat_id="ask my accountant"),
                             "jean@example.com")
    assert stripe.tax_ids == []


def test_checkout_invoices_the_top_up_and_taxes_it_exclusively(stripe):
    stripe_svc.create_topup_checkout("org-1", "cus_1", 50.0, "ref-abc", "Acme SAS")
    session = stripe.sessions[0]
    assert session["customer"] == "cus_1"
    # Exclusive: the customer pays 50 plus tax and the wallet is credited 50.
    # Inclusive would mean 50 euros of credits buys 41 euros of work.
    assert session["line_items"][0]["price_data"]["tax_behavior"] == "exclusive"
    assert session["automatic_tax"] == {"enabled": settings.stripe_automatic_tax}
    # A receipt carries no invoice number and no supplier registration number, so
    # a business customer cannot file it.
    assert session["invoice_creation"]["enabled"] is True
    invoice_data = session["invoice_creation"]["invoice_data"]
    assert invoice_data["account_tax_ids"] == ["txi_seller"]
    assert "Acme SAS" in invoice_data["footer"]
    assert session["tax_id_collection"] == {"enabled": True}
    # The one id that ties the later invoice.paid back to the ledger row this
    # session's own webhook writes.
    assert session["metadata"]["topup_ref"] == "ref-abc"
    assert invoice_data["metadata"]["topup_ref"] == "ref-abc"


def test_below_the_floor_never_reaches_stripe(stripe):
    with pytest.raises(stripe_svc.TopupTooSmall):
        stripe_svc.create_topup_checkout("org-1", "cus_1", stripe_svc.MIN_TOPUP - 0.5,
                                         "ref-abc")
    assert stripe.sessions == []


# --- which countries a registration actually covers -------------------------

def _registration(country, kind):
    return SimpleNamespace(country=country,
                           country_options={country.lower(): {"type": kind}})


def test_a_one_stop_shop_registration_covers_the_whole_eu(stripe, monkeypatch, caplog):
    """OSS is filed in ONE member state and collects across all 27 at each
    buyer's national rate. Reading its `country` alone reported twenty-six
    countries as uncovered that are covered, so every ordinary EU sale warned -
    and a log that cries wolf on the common case is one nobody reads when it is
    finally right. Verified against a live account: an EE oss_union registration
    charges a French consumer 20% and a German one 19%."""
    monkeypatch.setattr(
        stripe_svc.stripe_lib.tax.Registration, "list",
        lambda **k: SimpleNamespace(data=[_registration("EE", "oss_union")]),
        raising=False)
    covered = stripe_svc.tax_registrations()
    assert "EE" in covered and "FR" in covered and "DE" in covered
    assert set(countries.EU_MEMBERS) <= set(covered)
    # ...and it stops there. The UK left the EU, so an OSS registration says
    # nothing about it.
    assert "GB" not in covered

    with caplog.at_level("WARNING"):
        stripe_svc.warn_if_not_registered_for("FR")
    assert not caplog.records
    with caplog.at_level("WARNING"):
        stripe_svc.warn_if_not_registered_for("GB")
    assert any("selling into GB" in r.getMessage() for r in caplog.records)


def test_a_plain_registration_covers_only_its_own_country(stripe, monkeypatch):
    monkeypatch.setattr(
        stripe_svc.stripe_lib.tax.Registration, "list",
        lambda **k: SimpleNamespace(data=[_registration("CA", "standard")]),
        raising=False)
    assert stripe_svc.tax_registrations() == ["CA"]


def test_no_registrations_is_reported_not_shrugged_off(stripe, monkeypatch, caplog):
    """An account with none does not fail: it sells at zero tax and the
    liability accrues silently until a filing."""
    monkeypatch.setattr(stripe_svc.stripe_lib.tax.Registration, "list",
                        lambda **k: SimpleNamespace(data=[]), raising=False)
    with caplog.at_level("ERROR"):
        assert stripe_svc.tax_registrations() == []
    assert any("NO active tax registrations" in r.getMessage() for r in caplog.records)


def test_the_tax_total_is_summed_over_stacked_rates():
    """`total_taxes` is a list because rates stack - Quebec returns GST beside
    QST - so it is summed, never sampled. The flat `tax` field reads as null on a
    current API version, which books a taxed invoice as untaxed."""
    invoice = {"id": "in_1", "number": "A-0001",
               "hosted_invoice_url": "https://invoice.test/1",
               "invoice_pdf": "https://invoice.test/1.pdf",
               "total_taxes": [{"amount": 250}, {"amount": 499}]}
    fields = stripe_svc.invoice_fields(invoice)
    assert fields["tax_amount"] == 7.49
    assert fields["invoice_number"] == "A-0001"


# --- the account API --------------------------------------------------------

@pytest.fixture
def customer():
    email = f"profile-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "profile-secret-123"
    with SyncSession() as db:
        org = Organization(name="Profile Org", credit_balance=0.0)
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True, full_name="Jean Dupont"))
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


def _login(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_the_account_form_and_the_validator_agree_about_countries(client, customer):
    _, email, pwd = customer
    h = _login(client, email, pwd)
    served = client.get("/api/account/countries", headers=h).json()["countries"]
    assert {c["code"] for c in served} == set(countries.SUPPORTED)

    base = {"account_type": "individual", "full_name": "Jean Dupont"}

    # A country we cannot bill in is refused rather than stored: it would reach
    # Stripe in the middle of a payment.
    r = client.patch("/api/account", json={**base, "country": "JP"}, headers=h)
    assert r.status_code == 400, r.text

    r = client.patch("/api/account", json={
        **base, "country": "CA", "province": "XX"}, headers=h)
    assert r.status_code == 400, r.text


def test_a_move_abroad_drops_the_subdivision_it_left_behind(client, customer):
    """"ON" is a real Ontario and a real nothing-at-all in Germany. Cleared
    rather than refused: the stale value comes from the form the customer just
    moved away from, not from anything they typed."""
    org_id, email, pwd = customer
    h = _login(client, email, pwd)
    r = client.patch("/api/account", json={
        "account_type": "individual", "full_name": "Jean Dupont",
        "address_line1": "100 Queen St", "city": "Toronto", "postal_code": "M5H 2N2",
        "country": "CA", "province": "ON"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["org"]["province"] == "ON"
    assert r.json()["org"]["billing_address_missing"] == []

    r = client.patch("/api/account", json={
        "account_type": "individual", "full_name": "Jean Dupont",
        "address_line1": "1 rue de Rivoli", "city": "Paris", "postal_code": "75001",
        "country": "FR", "province": "ON"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["org"]["province"] is None
    assert r.json()["org"]["billing_address_missing"] == []


def test_an_individual_may_hold_a_billing_address_without_being_made_to(
        client, customer):
    """A personal account is perfectly ordinary without one, but it is still what
    the invoice is addressed to - so the fields exist for both types."""
    _, email, pwd = customer
    h = _login(client, email, pwd)
    r = client.patch("/api/account", json={
        "account_type": "individual", "full_name": "Jean Dupont"}, headers=h)
    assert r.status_code == 200, r.text
    # Reported, not refused: the customer is told what Stripe will not receive.
    assert set(r.json()["org"]["billing_address_missing"]) == {
        "line1", "city", "postal_code", "country"}

    r = client.patch("/api/account", json={
        "account_type": "individual", "full_name": "Jean Dupont",
        "address_line1": "1 rue de Rivoli", "city": "Paris",
        "postal_code": "75001", "country": "FR"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["org"]["billing_address_missing"] == []


def test_an_organization_in_a_state_billed_country_needs_its_state(client, customer):
    _, email, pwd = customer
    h = _login(client, email, pwd)
    r = client.patch("/api/account", json={
        "account_type": "organization", "full_name": "Jean Dupont",
        "company_name": "Dupont Inc", "address_line1": "1 Market St",
        "city": "San Francisco", "postal_code": "94105", "country": "US"}, headers=h)
    assert r.status_code == 400, r.text
    assert "state or province" in r.json()["detail"]


# --- the webhook ------------------------------------------------------------

def _webhook(client, monkeypatch, event):
    monkeypatch.setattr(stripe_svc, "parse_webhook", lambda payload, sig: event)
    return client.post("/api/billing/stripe/webhook", json={}, headers={})


def test_a_top_up_is_credited_once_and_its_invoice_stitched_on_later(
        client, customer, monkeypatch):
    """The wallet moves on the session event so the balance follows the money;
    the invoice is a separate Stripe object that arrives in a later webhook, so
    it is stitched on by `topup_ref` rather than waited for."""
    org_id, _, _ = customer
    session = {"type": "checkout.session.completed", "id": "evt_1",
               "data": {"object": {"id": "cs_test_topup_1", "metadata": {
                   "kind": "topup", "org_id": org_id, "amount": "50.0",
                   "topup_ref": "ref-xyz"}}}}
    assert _webhook(client, monkeypatch, session).status_code == 200
    # A redelivery is normal - Stripe retries on any non-2xx - and must not
    # credit twice.
    assert _webhook(client, monkeypatch, session).status_code == 200

    with SyncSession() as db:
        rows = db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == org_id)).scalars().all()
        assert len(rows) == 1
        assert rows[0].amount == 50.0
        assert rows[0].topup_ref == "ref-xyz"
        assert rows[0].invoice_number is None
        assert db.get(Organization, org_id).credit_balance == 50.0

    paid = {"type": "invoice.paid", "id": "evt_2", "data": {"object": {
        "id": "in_test_1", "number": "A-0007", "metadata": {
            "kind": "topup", "org_id": org_id, "topup_ref": "ref-xyz"},
        "hosted_invoice_url": "https://invoice.test/7",
        "invoice_pdf": "https://invoice.test/7.pdf",
        "total_taxes": [{"amount": 1000}]}}}
    assert _webhook(client, monkeypatch, paid).status_code == 200

    with SyncSession() as db:
        row = db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == org_id)).scalars().one()
        assert row.invoice_number == "A-0007"
        assert row.invoice_url == "https://invoice.test/7"
        assert row.tax_amount == 10.0
        # The wallet is credited the PRE-tax figure: the tax was charged on top.
        assert row.amount == 50.0


def test_the_address_typed_at_checkout_is_adopted(client, customer, monkeypatch):
    """Checkout overwrites the Stripe customer with what the cardholder typed,
    and that address is what the invoice was computed and printed from. Not
    copying it back leaves the account page showing one country while the filed
    invoice shows another."""
    org_id, _, _ = customer
    event = {"type": "checkout.session.completed", "id": "evt_3",
             "data": {"object": {"id": "cs_test_topup_2", "metadata": {
                 "kind": "topup", "org_id": org_id, "amount": "10.0",
                 "topup_ref": "ref-2"},
                 "customer_details": {"address": {
                     "line1": "10 Downing St", "city": "London",
                     "postal_code": "SW1A 2AA", "country": "GB", "state": "England"}}}}}
    assert _webhook(client, monkeypatch, event).status_code == 200
    with SyncSession() as db:
        org = db.get(Organization, org_id)
        assert org.country == "GB"
        assert org.city == "London"
        # The UK bills nationally, so a "state" Stripe happened to collect is
        # not a subdivision we can place.
        assert org.province is None


def test_a_payment_from_a_country_we_do_not_bill_in_is_not_adopted(
        client, customer, monkeypatch):
    """The payment stands - it already went through - but adopting the address
    would put the account into a state the account form cannot express."""
    org_id, _, _ = customer
    event = {"type": "checkout.session.completed", "id": "evt_4",
             "data": {"object": {"id": "cs_test_topup_3", "metadata": {
                 "kind": "topup", "org_id": org_id, "amount": "10.0",
                 "topup_ref": "ref-3"},
                 "customer_details": {"address": {
                     "line1": "1 Chome", "city": "Tokyo",
                     "postal_code": "100-0001", "country": "JP"}}}}}
    assert _webhook(client, monkeypatch, event).status_code == 200
    with SyncSession() as db:
        org = db.get(Organization, org_id)
        assert org.country is None
        assert org.credit_balance == 10.0
