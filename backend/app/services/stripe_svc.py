"""Stripe: credit top-ups (Checkout) and quote payment links (PROMPT §18).

Every path that moves money issues an INVOICE, never a bare charge. A Stripe
receipt carries no invoice number, no supplier registration number and no tax
line, so a business customer cannot file it: claiming input VAT wants the
supplier's number on the document, and a receipt does not have one. That is why
both Checkout paths run with `invoice_creation`.

The invoice renders from the Stripe CUSTOMER, not from the session, so the legal
name, the billing address and the customer's own tax number are pushed across
before a session is created. A billing profile that lives only in Postgres
produces an invoice addressed to nobody - which is precisely what this
deployment used to do.

A deployment with placeholder keys is a supported state: every entry point
raises StripeUnavailable, the API answers 503, and credits are granted by hand
instead. That keeps local development free of a Stripe account.
"""
import json
import logging
from uuid import uuid4

import stripe as stripe_lib

from app.core.config import settings
from app.services import countries

log = logging.getLogger(__name__)

# Floor for a credit top-up, in `settings.credit_currency`. Stripe's own minimum
# charge (~0.50 EUR) is lower, but a top-up smaller than this costs more in card
# fees than it is worth, so the amount is rejected before a Checkout session is
# ever created. The SPA reads it from GET /billing/balance so both sides agree.
MIN_TOPUP = 2.0

# Resolved account tax ids, cached for the life of the process. Only a non-empty
# answer is cached: an operator who adds the registration number to the Stripe
# dashboard mid-flight should not have to restart the api to stop shipping
# invoices that are missing it.
_account_tax_id_cache: list[str] = []
# None until read: an empty list is a real answer here, so it cannot be the sentinel.
_tax_registration_cache: list[str] | None = None


class StripeUnavailable(Exception):
    pass


class TopupTooSmall(Exception):
    pass


def configured() -> bool:
    key = settings.stripe_secret_key
    return key.startswith("sk_") and "placeholder" not in key


def _api():
    if not configured():
        raise StripeUnavailable("Stripe is not configured on this deployment")
    stripe_lib.api_key = settings.stripe_secret_key
    return stripe_lib


def _line_description() -> str:
    return f"{settings.brand_name} credits top-up"


def _invoice_footer(legal_name: str | None = None) -> str:
    """The line under every invoice, naming who actually issued it.

    `legal_name` is the admin-set legal identity (§legal identity, the same one
    the landing's Terms and Privacy pages carry); the brand is the fallback for
    a deployment that has not filled it in. A rebranded install is operated by
    someone else and its invoices have to say so.
    """
    entity = (legal_name or "").strip().rstrip(". ") or settings.brand_name
    return (f"Issued by {entity}. Credits are drawn down as work is delivered, at "
            f"the rates published on {settings.app_base_url}/billing.")


def tax_registrations() -> list[str] | None:
    """Country codes the Stripe account is actually registered to collect in.

    None means the question could not be answered, which is different from an
    empty list and must not be treated as one.

    This exists because there is no guard anywhere else. A Checkout session
    created with `automatic_tax` enabled and no registration covering the buyer
    returns 200 and a tax total of zero - it does not fail. The only thing that
    fails loudly is a missing head office address under Tax > Settings, which is
    a different misconfiguration and is easy to mistake for this one being fine.
    So the noise has to come from here. Read once and cached, because it changes
    about as often as the company does.
    """
    global _tax_registration_cache
    if _tax_registration_cache is not None:
        return _tax_registration_cache
    s = _api()
    try:
        found = sorted({(r.country or "").upper()
                        for r in s.tax.Registration.list(status="active", limit=100).data
                        if getattr(r, "country", None)})
    except AttributeError:
        # An older stripe library has no tax.Registration resource. Unknown, not
        # empty: reporting "no registrations" because we cannot see them would
        # cry wolf on a correctly configured account.
        log.info("this stripe library exposes no tax.Registration; "
                 "cannot verify where the account is registered to collect")
        return None
    except stripe_lib.error.StripeError as exc:
        log.warning("could not read tax registrations from Stripe: %s", exc)
        return None
    if not found:
        log.error("Stripe account has NO active tax registrations; automatic_tax will "
                  "return zero tax on every sale and the liability accrues silently "
                  "until a filing. Configure Tax > Registrations for this account AND "
                  "this mode")
    _tax_registration_cache = found
    return found


def warn_if_not_registered_for(country: str | None) -> None:
    """Say so when we are about to sell into a country we cannot collect for.

    A warning rather than a refusal on purpose. Refusing here would take the
    payment path down the moment a customer appears from a country the operator
    has not registered in yet, and that decision belongs to whoever reads the
    log, not to a release.
    """
    if not settings.stripe_automatic_tax or not country:
        return
    registered = tax_registrations()
    if not registered:
        return  # already reported by tax_registrations, at the right severity
    if country.upper() not in registered:
        log.warning("selling into %s with no active Stripe tax registration there "
                    "(registered: %s); this sale will be taxed at zero",
                    country.upper(), ", ".join(registered))


def account_tax_ids() -> list[str]:
    """The seller's own registration numbers, for the invoice header.

    These live in the Stripe dashboard rather than in this file on purpose: they
    are account identity, and the test and live accounts hold different ones, so
    a value committed here would be wrong in one of the two environments. An
    account with none configured still issues invoices - it just issues ones a
    business customer cannot reclaim VAT against, which is worth a warning on
    every call rather than a note in a log nobody greps.
    """
    global _account_tax_id_cache
    if _account_tax_id_cache:
        return _account_tax_id_cache
    s = _api()
    try:
        # "self", not "account": the latter is for a Connect account and wants an
        # explicit id, so it 400s on the platform's own tax ids.
        found = [t.id for t in s.TaxId.list(owner={"type": "self"}, limit=10).data]
    except stripe_lib.error.StripeError as exc:
        log.warning("could not read account tax ids from Stripe: %s", exc)
        return []
    if not found:
        log.warning("Stripe account has no tax id configured; invoices will go out "
                    "without a supplier registration number and customers will not "
                    "be able to reclaim the tax against them")
        return []
    _account_tax_id_cache = found
    return found


def billing_name(org) -> str:
    """Who the invoice is made out to.

    Follows the account TYPE, not merely whichever field happens to be filled
    in. An organization that switches back to individual keeps its stored
    company details on purpose (a round-trip must not lose them), so reading
    `company_name or name` addressed a personal invoice to a company the account
    no longer says it is.
    """
    if getattr(org, "type", None) == "organization":
        return (org.company_name or org.name or "").strip()
    return (org.name or "").strip()


def billing_tax_number(org) -> str:
    """The customer's tax number, if this account is one that can hold it.

    Gated on the account type for the same reason the name is, and with sharper
    teeth: in the EU a customer VAT number moves the transaction to reverse
    charge and the invoice is issued with NO tax at all. A company number left
    behind by an account that has since switched to individual would therefore
    not merely look odd on a personal invoice - it would zero the VAT on it.
    """
    if getattr(org, "type", None) != "organization":
        return ""
    return (org.vat_id or "").strip()


def _address_of(org) -> dict | None:
    """The org's billing address in Stripe's shape, or None when incomplete.

    Stripe Tax resolves a rate from this address, and a partial one resolves to
    the WRONG rate rather than to an error, so anything short of a full address
    is withheld and Checkout collects it instead.

    `state` goes only to countries that bill by one. Sending a subdivision to a
    country with a national rate is at best ignored and at worst a rejected
    address, and the EU is the whole reason that matters.
    """
    missing = countries.missing_address_fields(org)
    if missing:
        # Withheld, not sent partial: a US or Canadian address without its state
        # resolves to a federal rate and misses the other half of the bill. Said
        # out loud, because a silent withhold looks identical to a customer who
        # simply has not bought anything yet.
        log.warning(
            "org %s has an incomplete billing address (missing %s); nothing sent to "
            "Stripe, so Checkout will not prefill and tax is computed from whatever "
            "the buyer types", org.id, ", ".join(missing))
        return None
    address = {
        "line1": org.address_line1,
        "line2": org.address_line2 or None,
        "city": org.city,
        "postal_code": org.postal_code,
        "country": org.country,
    }
    if countries.needs_subdivision(org.country):
        address["state"] = org.province
    return address


def sync_customer(customer_id: str, org, email: str) -> None:
    """Push the account's billing profile onto the Stripe customer.

    Called before every payment rather than once at creation, because the
    customer edits these on the account page long after the Stripe customer
    exists, and the invoice is rendered from whatever Stripe holds at the moment
    it is issued.
    """
    s = _api()
    payload: dict = {"email": email}
    name = billing_name(org)
    if name:
        payload["name"] = name
    address = _address_of(org)
    if address:
        payload["address"] = address
    try:
        s.Customer.modify(customer_id, **payload)
    except stripe_lib.error.StripeError as exc:
        # Never block a payment on a profile sync; the invoice is still issued,
        # it just renders with whatever Stripe already held.
        log.warning("could not sync billing profile to customer %s: %s", customer_id, exc)
        return

    # Every payment path calls this before charging, so it is the one place that
    # sees every sale and knows the buyer's country.
    warn_if_not_registered_for(org.country)

    number = billing_tax_number(org)
    if not number:
        return
    resolved = countries.tax_id_for(org.country, number)
    if resolved is None:
        log.info("tax number on org %s is not recognisable for %s; not sent to Stripe",
                 org.id, org.country)
        return
    tax_type, value = resolved
    try:
        existing = s.TaxId.list(owner={"type": "customer", "customer": customer_id},
                                limit=10).data
        if any((t.value or "").upper() == value.upper() for t in existing):
            return
        # Stripe has no update for a tax id, and Checkout skips collection
        # entirely once a customer holds one, so a stale number would otherwise
        # be pinned to the account forever.
        for stale in existing:
            s.TaxId.delete(stale.id)
        s.TaxId.create(type=tax_type, value=value,
                       owner={"type": "customer", "customer": customer_id})
    except stripe_lib.error.StripeError as exc:
        log.warning("could not set tax id on customer %s: %s", customer_id, exc)


def ensure_customer(org_id: str, email: str, name: str | None,
                    existing_id: str | None) -> str:
    """The Stripe customer for an organization, created on first need.

    A customer that outlives one session is what carries the billing profile,
    and it is what the invoice history in the portal is keyed on.
    """
    s = _api()
    if existing_id:
        try:
            customer = s.Customer.retrieve(existing_id)
            if not getattr(customer, "deleted", False):
                return existing_id
        except stripe_lib.error.StripeError:
            log.warning("stripe customer %s not retrievable; creating a new one", existing_id)
    return s.Customer.create(email=email, name=name or email,
                             metadata={"org_id": org_id}).id


def create_topup_checkout(org_id: str, customer_id: str, amount: float,
                          topup_ref: str, legal_name: str | None = None) -> str:
    """Hosted Checkout for a one-off top-up, invoiced.

    Tax is EXCLUSIVE: the customer pays `amount` plus whatever Stripe Tax
    computes, and the wallet is credited `amount`. Taking the tax out of the
    credit instead would mean a customer who buys a hundred euros of credits can
    only spend eighty of it, and every published price would be wrong by the
    local rate.
    """
    if amount < MIN_TOPUP:
        raise TopupTooSmall(f"The minimum top-up is {MIN_TOPUP:g} "
                            f"{settings.credit_currency}")
    s = _api()
    try:
        session = _topup_session(s, org_id, customer_id, amount, topup_ref, legal_name)
    except stripe_lib.error.StripeError as exc:
        # Everything a misconfigured account can throw arrives here - most
        # usefully "you must have a valid head office address to enable
        # automatic tax calculation", which is Stripe Tax not being set up yet.
        # Surfaced as the one exception the API layer knows how to answer, so it
        # becomes a 503 with the reason rather than a 500 with a traceback.
        raise StripeUnavailable(str(getattr(exc, "user_message", None) or exc)) from exc
    return session.url


def _topup_session(s, org_id: str, customer_id: str, amount: float,
                   topup_ref: str, legal_name: str | None):
    return s.checkout.Session.create(
        mode="payment",
        customer=customer_id,
        line_items=[{
            "price_data": {
                "currency": settings.credit_currency.lower(),
                "product_data": {
                    "name": _line_description(),
                    "tax_code": settings.stripe_tax_code,
                },
                "unit_amount": int(round(amount * 100)),
                "tax_behavior": "exclusive",
            },
            "quantity": 1,
        }],
        automatic_tax={"enabled": settings.stripe_automatic_tax},
        # Checkout renders the invoice from the customer's saved address unless
        # told to take what was typed at the till, and the typed one is the one
        # the cardholder just asserted is their billing address.
        customer_update={"address": "auto", "name": "auto"},
        billing_address_collection="required",
        tax_id_collection={"enabled": True},
        invoice_creation={
            "enabled": True,
            "invoice_data": {
                "description": _line_description(),
                "footer": _invoice_footer(legal_name),
                "account_tax_ids": account_tax_ids() or None,
                # Copied onto the invoice so `invoice.paid` can find the ledger
                # row that `checkout.session.completed` already wrote.
                "metadata": {"org_id": org_id, "kind": "topup",
                             "amount": str(amount), "topup_ref": topup_ref},
            },
        },
        metadata={"org_id": org_id, "kind": "topup", "amount": str(amount),
                  "topup_ref": topup_ref},
        success_url=f"{settings.app_base_url}/billing?status=success",
        cancel_url=f"{settings.app_base_url}/billing?status=canceled",
    )


def create_quote_payment_link(quote_id: str, amount: float, description: str,
                              legal_name: str | None = None) -> str:
    """A payment link for a quote settled outside the credit wallet.

    Invoiced and taxed like a top-up, for the same reason: this is the path a
    direct engagement is actually paid through, so it is the one most likely to
    need a document an accountant will accept. The link has no customer of its
    own - whoever opens it types their details at Checkout, and Stripe Tax
    computes from those.
    """
    s = _api()
    try:
        price = s.Price.create(
            currency=settings.credit_currency.lower(),
            unit_amount=int(round(amount * 100)),
            tax_behavior="exclusive",
            product_data={"name": description[:120]},
        )
        link = s.PaymentLink.create(
            line_items=[{"price": price.id, "quantity": 1}],
            automatic_tax={"enabled": settings.stripe_automatic_tax},
            billing_address_collection="required",
            tax_id_collection={"enabled": True},
            # A link can be opened by anyone, so the customer is created when it
            # is paid - otherwise the invoice below has nobody to address.
            customer_creation="always",
            invoice_creation={
                "enabled": True,
                "invoice_data": {
                    "description": description[:500],
                    "footer": _invoice_footer(legal_name),
                    "account_tax_ids": account_tax_ids() or None,
                    "metadata": {"quote_id": quote_id, "kind": "quote"},
                },
            },
            metadata={"quote_id": quote_id, "kind": "quote"},
        )
    except stripe_lib.error.StripeError as exc:
        raise StripeUnavailable(str(getattr(exc, "user_message", None) or exc)) from exc
    return link.url


def _field(invoice, name):
    """Read one field off an invoice that may be an object or a webhook dict.

    `invoice_fields` is fed both: a StripeObject from a direct API call, and the
    plain dict `parse_webhook` re-parses out of the verified payload.
    """
    if isinstance(invoice, dict):
        return invoice.get(name)
    return getattr(invoice, name, None)


def tax_total_cents(invoice) -> int:
    """Tax charged on top of the line, in cents.

    Invoices carry this as `total_taxes`, a list with one entry per rate - a
    single top-up is one line, but a jurisdiction that stacks a federal and a
    provincial rate produces two, and taking only the first would under-report
    the total. The flat `tax` field is the older spelling: it reads as null on a
    current API version, which silently books every invoice as untaxed.
    """
    rows = _field(invoice, "total_taxes")
    if rows:
        return sum(int(_field(r, "amount") or 0) for r in rows)
    return int(_field(invoice, "tax") or 0)


def invoice_fields(invoice) -> dict:
    """The parts of an invoice the ledger keeps.

    Stripe counts in cents; the tax is what the customer paid ON TOP of the
    credited amount, which is why it is stored rather than derived from the
    ledger row.
    """
    return {
        "invoice_id": _field(invoice, "id"),
        "invoice_number": _field(invoice, "number"),
        "invoice_url": _field(invoice, "hosted_invoice_url"),
        "invoice_pdf": _field(invoice, "invoice_pdf"),
        "tax_amount": round(tax_total_cents(invoice) / 100.0, 6),
    }


def fetch_invoice(invoice_id: str) -> dict | None:
    """Read an invoice by id, for filling in a ledger row after the fact."""
    s = _api()
    try:
        return invoice_fields(s.Invoice.retrieve(invoice_id))
    except stripe_lib.error.StripeError as exc:
        log.warning("could not retrieve invoice %s: %s", invoice_id, exc)
        return None


def new_topup_ref() -> str:
    """Our own id for one top-up, carried on both the session and the invoice.

    The two arrive in separate webhooks under different Stripe ids, so this is
    what lets the later `invoice.paid` find the ledger row that the earlier
    `checkout.session.completed` already wrote.
    """
    return uuid4().hex


# --- The customer portal -----------------------------------------------------
#
# Where a customer reads back everything they have been invoiced for. Stripe
# hosts it, which is what makes it worth having: an invoice reissued, corrected
# or credited after the fact shows up there without us mirroring anything.
#
# The portal configuration is OURS, resolved once per process, never Stripe's
# default. A session created without a configuration dies on an account where
# nobody has opened Settings > Billing > Customer portal, and the default
# configuration carries features (cancel, change plan, edit the card) that
# belong to pages which decide who may use them. So it is read-only by
# construction: invoice history and nothing else.
_portal_config_cache: str | None = None

# Matched by metadata rather than held in an env var: a configuration id means a
# different thing in sandbox than in live, and an env var holding one gets
# copied between modes exactly once.
PORTAL_CONFIG_KEY = "openvisor"
PORTAL_CONFIG_TAG = "invoices_v1"


def _portal_configuration(s) -> str:
    global _portal_config_cache
    if _portal_config_cache:
        return _portal_config_cache
    for cfg in s.billing_portal.Configuration.list(limit=100, active=True).auto_paging_iter():
        # `_field` twice, not `.get`: a StripeObject is not a dict, and calling
        # `.get` on one raises AttributeError - which is not a StripeError, so
        # it would walk past the handler in `create_portal_session` and reach
        # the browser as a 500 with no message at all.
        if _field(_field(cfg, "metadata"), PORTAL_CONFIG_KEY) == PORTAL_CONFIG_TAG:
            _portal_config_cache = cfg.id
            return cfg.id
    cfg = s.billing_portal.Configuration.create(
        # Stripe refuses the call without these unless the account already
        # carries them, and the account is not something this repo can set.
        business_profile={
            "headline": f"{settings.brand_name} invoices",
            "privacy_policy_url": f"{settings.landing_base_url}/privacy",
            "terms_of_service_url": f"{settings.landing_base_url}/terms",
        },
        features={
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": False},
            "customer_update": {"enabled": False},
        },
        metadata={PORTAL_CONFIG_KEY: PORTAL_CONFIG_TAG},
    )
    _portal_config_cache = cfg.id
    log.info("created stripe billing portal configuration %s", cfg.id)
    return cfg.id


def create_portal_session(customer_id: str, return_url: str) -> str:
    """A one-time link into this customer's invoice history.

    The URL is short-lived and single-customer, so it is handed straight to the
    browser rather than stored anywhere.
    """
    s = _api()
    try:
        session = s.billing_portal.Session.create(
            customer=customer_id,
            configuration=_portal_configuration(s),
            return_url=return_url,
        )
    except stripe_lib.error.StripeError as exc:
        raise StripeUnavailable(str(getattr(exc, "user_message", None) or exc)) from exc
    return session.url


def parse_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify the signature and return the event as plain dicts (stripe-python
    v15 StripeObject is no longer a dict subclass, so .get() raises on it)."""
    stripe_lib.api_key = settings.stripe_secret_key
    stripe_lib.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    return json.loads(payload)
