"""Stripe: credit top-ups (Checkout) and quote payment links (PROMPT §18)."""
import json
import logging

import stripe as stripe_lib

from app.core.config import settings

log = logging.getLogger(__name__)


class StripeUnavailable(Exception):
    pass


def _configured() -> bool:
    return settings.stripe_secret_key.startswith("sk_") and "placeholder" not in settings.stripe_secret_key


def create_topup_checkout(org_id: str, amount_eur: float) -> str:
    if not _configured():
        raise StripeUnavailable("Stripe is not configured")
    stripe_lib.api_key = settings.stripe_secret_key
    session = stripe_lib.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": settings.credit_currency.lower(),
                "product_data": {"name": f"{settings.brand_name} credits top-up"},
                "unit_amount": int(round(amount_eur * 100)),
            },
            "quantity": 1,
        }],
        metadata={"org_id": org_id, "kind": "topup", "amount": str(amount_eur)},
        success_url=f"{settings.app_base_url}/billing?status=success",
        cancel_url=f"{settings.app_base_url}/billing?status=canceled",
    )
    return session.url


def create_quote_payment_link(quote_id: str, amount_eur: float, description: str) -> str:
    if not _configured():
        raise StripeUnavailable("Stripe is not configured")
    stripe_lib.api_key = settings.stripe_secret_key
    price = stripe_lib.Price.create(
        currency=settings.credit_currency.lower(),
        unit_amount=int(round(amount_eur * 100)),
        product_data={"name": description[:120]},
    )
    link = stripe_lib.PaymentLink.create(
        line_items=[{"price": price.id, "quantity": 1}],
        metadata={"quote_id": quote_id, "kind": "quote"},
    )
    return link.url


def parse_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify the signature and return the event as plain dicts (stripe-python
    v15 StripeObject is no longer a dict subclass, so .get() raises on it)."""
    stripe_lib.api_key = settings.stripe_secret_key
    stripe_lib.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    return json.loads(payload)
