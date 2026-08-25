import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.serializers import quote_out
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, get_project_for_user
from app.models import (
    CreditTransaction, Organization, Project, Quote, QuoteAttachment, User, utcnow,
)
from app.schemas.schemas import QuoteDecisionIn, TopupIn
from app.services import app_settings, brand, countries, stripe_svc
from app.services.sysmsg import post_system_message

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/balance")
async def balance(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    org = await db.get(Organization, user.org_id)
    return {"credit_balance": round(org.credit_balance or 0.0, 4),
            "currency": settings.credit_currency,
            # The top-up floor travels with the balance so the SPA's amount field
            # can't offer an amount the checkout call would reject.
            "min_topup": stripe_svc.MIN_TOPUP}


@router.get("/transactions")
async def transactions(user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CreditTransaction)
                             .where(CreditTransaction.org_id == user.org_id)
                             .order_by(CreditTransaction.created_at.desc()).limit(200))
            ).scalars().all()
    return [{"id": t.id, "project_id": t.project_id, "amount": round(t.amount, 4),
             "kind": t.kind, "detail": t.detail, "created_at": t.created_at,
             # §18 invoicing: present on a top-up once Stripe has issued the
             # document, null everywhere else. `tax_amount` is what the card was
             # charged ON TOP of `amount` - the wallet is credited the pre-tax
             # figure, so the ledger would otherwise not remember the difference.
             "invoice_number": t.invoice_number, "invoice_url": t.invoice_url,
             "invoice_pdf": t.invoice_pdf,
             "tax_amount": round(t.tax_amount, 4) if t.tax_amount is not None else None}
            for t in rows]


@router.post("/topup")
async def topup(body: TopupIn, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db)):
    org = await db.get(Organization, user.org_id)
    try:
        customer_id = stripe_svc.ensure_customer(
            org.id, user.email, stripe_svc.billing_name(org), org.stripe_customer_id)
        if customer_id != org.stripe_customer_id:
            org.stripe_customer_id = customer_id
            await db.commit()
        # The invoice renders from the Stripe CUSTOMER, so the legal name, the
        # billing address and the customer's tax number have to be over there
        # BEFORE the session is created, not merely in our database. This used
        # to be collected and never sent, which is how a paying organization
        # received a receipt addressed to nobody.
        stripe_svc.sync_customer(customer_id, org, user.email)
        legal = await app_settings.get_legal_identity(db)
        url = stripe_svc.create_topup_checkout(
            org.id, customer_id, body.amount, stripe_svc.new_topup_ref(),
            legal["legal_name"])
    except stripe_svc.TopupTooSmall as exc:
        raise HTTPException(400, str(exc))
    except stripe_svc.StripeUnavailable as exc:
        raise HTTPException(503, f"Card payments are not available on this "
                                 f"deployment ({exc})")
    return {"checkout_url": url}


@router.post("/portal")
async def portal(user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    """A Stripe-hosted link to every invoice this account has been issued.

    Stripe hosts it on purpose: an invoice corrected or credited after it was
    issued stays correct there, which a copy of ours could not be. The ledger
    row links the invoice for the payment that created it; this is where the
    whole history lives.
    """
    org = await db.get(Organization, user.org_id)
    if not stripe_svc.configured():
        raise HTTPException(503, "Card payments are not configured on this deployment")
    if not org.stripe_customer_id:
        # No customer means no payment has ever been taken, so there is no
        # portal rather than an empty one. The SPA knows this from
        # `org.stripe_customer` and should not have offered the button.
        raise HTTPException(404, "No payments on file yet. Invoices appear here "
                                 "after the first one.")
    try:
        url = stripe_svc.create_portal_session(
            org.stripe_customer_id, f"{settings.app_base_url}/billing")
    except stripe_svc.StripeUnavailable as exc:
        raise HTTPException(503, str(exc))
    return {"portal_url": url}


@router.post("/stripe/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe_svc.parse_webhook(payload, sig)
    except Exception:
        # Loud on purpose: production runs uvicorn without its access log, so a
        # delivery signed with the wrong secret otherwise leaves no trace at
        # all - Stripe sees a 400, we see nothing, and the wallet stays empty
        # after a card was charged.
        log.warning("stripe webhook rejected: bad signature (%d bytes)", len(payload))
        raise HTTPException(400, "Invalid webhook signature")

    kind = event.get("type")
    # One line per delivery, whatever it turns out to be. A handled event that
    # changes nothing is otherwise silent, and "silent" and "never arrived" have
    # to be tellable apart from the logs.
    log.info("stripe webhook %s %s", kind, event.get("id"))
    if kind == "checkout.session.completed":
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        if meta.get("kind") == "topup":
            await _credit_topup(db, meta, obj["id"])
            # `customer_update.address: auto` means Checkout overwrote the
            # Stripe customer with what the cardholder typed, and THAT address
            # is what the invoice was computed and printed from.
            await _adopt_billing_address(db, meta.get("org_id"),
                                         obj.get("customer_details"))
            # Usually null at this point - Checkout issues the invoice after the
            # payment settles, so `invoice.paid` is what normally fills it in.
            if obj.get("invoice"):
                await _attach_invoice(db, meta.get("topup_ref"), obj["invoice"])
        elif meta.get("kind") == "quote":
            quote = await db.get(Quote, meta["quote_id"])
            if quote:
                quote.status = "paid"
                await db.commit()
    elif kind == "invoice.paid":
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        if meta.get("kind") == "topup":
            await _attach_invoice(db, meta.get("topup_ref"), obj["id"], obj)
    return {"ok": True}


async def _credit_topup(db: AsyncSession, meta: dict, ref: str) -> None:
    """Apply a paid top-up exactly once, then unblock anything waiting on it."""
    org = await db.get(Organization, meta.get("org_id", ""))
    if org is None:
        # Several dev instances can listen on the same Stripe sandbox; an event
        # for another instance's org is not ours to credit.
        log.warning("ignoring topup webhook for unknown org %s", meta.get("org_id"))
        return
    seen = (await db.execute(select(CreditTransaction).where(
        CreditTransaction.stripe_ref == ref))).scalars().first()
    if seen is not None:
        # A redelivery of an event we have already banked. Stripe retries on any
        # non-2xx, so this is a normal occurrence, not an anomaly.
        return
    amount = float(meta.get("amount") or 0)
    if amount <= 0:
        log.warning("ignoring topup webhook with no amount for org %s", org.id)
        return
    org.credit_balance = round((org.credit_balance or 0.0) + amount, 6)
    db.add(CreditTransaction(org_id=org.id, amount=amount, kind="topup",
                             stripe_ref=ref,
                             # Carried from the Stripe metadata so `invoice.paid`
                             # can find this row later; without it the invoice
                             # has no way back to the ledger.
                             topup_ref=meta.get("topup_ref"),
                             detail="Credit top-up"))
    await db.commit()
    log.info("credited %.2f %s to org %s", amount, settings.credit_currency, org.id)

    from app.workers.celery_app import celery
    pending = (await db.execute(select(Project.id).where(
        Project.org_id == org.id, Project.status == "payment_due"))).all()
    for (pid,) in pending:
        celery.send_task("app.workers.tasks.maybe_start_development", args=[pid])


async def _adopt_billing_address(db: AsyncSession, org_id: str | None,
                                 details: dict | None) -> None:
    """Take the address the cardholder just typed at Checkout.

    Not copying it back leaves the account page showing one country while the
    customer's invoice shows another - and the invoice is the one that was
    filed. Only ever widens what we know: a field Checkout did not collect is
    left alone rather than blanked.
    """
    address = (details or {}).get("address") or {}
    if not org_id or not address.get("country"):
        return
    org = await db.get(Organization, org_id)
    if org is None:
        return
    country = (address["country"] or "").upper()
    if not countries.is_supported(country):
        # Stripe will happily take a card from a country we do not bill in. The
        # payment stands - it already went through - but adopting the address
        # would put the account into a state the account form cannot express.
        log.warning("checkout for org %s returned unsupported billing country %s; "
                    "address not adopted", org_id, country)
        return
    org.country = country
    org.address_line1 = address.get("line1") or org.address_line1
    org.address_line2 = address.get("line2") or org.address_line2
    org.city = address.get("city") or org.city
    org.postal_code = address.get("postal_code") or org.postal_code
    # Cleared for a country that bills nationally, so a leftover province from
    # the previous address cannot follow the customer abroad.
    org.province = (address.get("state") or None) \
        if countries.needs_subdivision(country) else None
    await db.commit()
    log.info("adopted billing address from checkout for org %s (%s)", org_id, country)


async def _attach_invoice(db: AsyncSession, topup_ref: str | None,
                          invoice_id: str, obj: dict | None = None) -> None:
    """Record the invoice against the ledger row its payment created.

    The wallet is credited on the session event so the balance moves as soon as
    the money does; the invoice is a separate Stripe object that arrives later,
    so it is stitched on here rather than waited for. A row that never gets one
    still shows the payment - it just has no document to link.
    """
    if not topup_ref:
        return
    row = (await db.execute(select(CreditTransaction).where(
        CreditTransaction.topup_ref == topup_ref,
        CreditTransaction.invoice_number.is_(None)))).scalars().first()
    if row is None:
        return
    # The webhook already carries the whole invoice, so the event body is read
    # directly and only a bare id costs a round trip. `invoice_fields` takes
    # either shape, which keeps one definition of where the tax total lives.
    fields = stripe_svc.invoice_fields(obj) if obj is not None \
        else stripe_svc.fetch_invoice(invoice_id)
    if not fields:
        return
    row.invoice_number = fields.get("invoice_number")
    row.invoice_url = fields.get("invoice_url")
    row.invoice_pdf = fields.get("invoice_pdf")
    row.tax_amount = fields.get("tax_amount")
    await db.commit()
    log.info("attached invoice %s to top-up %s", fields.get("invoice_number"), topup_ref)


quotes_router = APIRouter(prefix="/api/projects/{project_id}/quotes", tags=["billing"])


@quotes_router.get("")
async def project_quotes(project: Project = Depends(get_project_for_user),
                         db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Quote).where(Quote.project_id == project.id)
                             .options(selectinload(Quote.attachments))
                             .order_by(Quote.created_at.desc()))).scalars().all()
    return [quote_out(q) for q in rows]


async def _get_quote(db: AsyncSession, project: Project, quote_id: str) -> Quote:
    quote = (await db.execute(select(Quote).where(
        Quote.id == quote_id, Quote.project_id == project.id)
        .options(selectinload(Quote.attachments)))).scalar_one_or_none()
    if quote is None:
        raise HTTPException(404, "Quote not found")
    return quote


@quotes_router.post("/{quote_id}/accept")
async def accept_quote(quote_id: str, body: QuoteDecisionIn,
                       project: Project = Depends(get_project_for_user),
                       db: AsyncSession = Depends(get_db)):
    """Accept a credit quote: charges the org wallet and commits the consultant to
    deliver the quoted work to the customer repo."""
    from app.workers.celery_app import celery  # late import (avoid cycle)

    quote = await _get_quote(db, project, quote_id)
    if quote.price_credits is None:
        raise HTTPException(409, "This quote is settled via its payment link, not credits")
    if quote.status != "sent":
        raise HTTPException(409, f"Quote is {quote.status}; only a sent quote can be accepted")
    org = await db.get(Organization, project.org_id)
    if (org.credit_balance or 0.0) < quote.price_credits:
        raise HTTPException(402, f"This quote costs {quote.price_credits:g} credits; "
                                 f"your balance is too low. Top up to accept it.")
    org.credit_balance = (org.credit_balance or 0.0) - quote.price_credits
    db.add(CreditTransaction(org_id=org.id, project_id=project.id,
                             amount=-quote.price_credits, kind="quote",
                             detail=f"Accepted quote: {quote.title}"))
    quote.status = "accepted"
    quote.decision_comment = body.comment
    quote.decided_at = utcnow()
    await post_system_message(
        db, project.id,
        f"Quote accepted: {quote.title} ({quote.price_credits:g} credits)"
        + (f" - {body.comment}" if body.comment else ""))
    await db.commit()
    celery.send_task("app.workers.tasks.send_email", args=[
        settings.admin_email,
        brand.subject(f"Quote accepted: {quote.title}"),
        f"The customer accepted the quote '{quote.title}' "
        f"({quote.price_credits:g} credits) on project '{project.name}'.\n"
        + (f"Comment: {body.comment}\n" if body.comment else "")
        + f"{settings.app_base_url}/projects/{project.id}\n\n"
        f"This commits you to deliver it to the customer repo."])
    return quote_out(quote)


@quotes_router.post("/{quote_id}/deny")
async def deny_quote(quote_id: str, body: QuoteDecisionIn,
                     project: Project = Depends(get_project_for_user),
                     db: AsyncSession = Depends(get_db)):
    from app.workers.celery_app import celery

    quote = await _get_quote(db, project, quote_id)
    if quote.price_credits is None:
        raise HTTPException(409, "This quote is settled via its payment link, not credits")
    if quote.status != "sent":
        raise HTTPException(409, f"Quote is {quote.status}; only a sent quote can be denied")
    quote.status = "denied"
    quote.decision_comment = body.comment
    quote.decided_at = utcnow()
    await post_system_message(
        db, project.id,
        f"Quote denied: {quote.title}" + (f" - {body.comment}" if body.comment else ""))
    await db.commit()
    celery.send_task("app.workers.tasks.send_email", args=[
        settings.admin_email,
        brand.subject(f"Quote denied: {quote.title}"),
        f"The customer denied the quote '{quote.title}' on project '{project.name}'.\n"
        + (f"Comment: {body.comment}\n" if body.comment else "")
        + f"{settings.app_base_url}/projects/{project.id}"])
    return quote_out(quote)


@quotes_router.get("/{quote_id}/attachments/{attachment_id}")
async def download_attachment(quote_id: str, attachment_id: str,
                              project: Project = Depends(get_project_for_user),
                              db: AsyncSession = Depends(get_db)):
    quote = await _get_quote(db, project, quote_id)
    att = next((a for a in quote.attachments if a.id == attachment_id), None)
    if att is None:
        raise HTTPException(404, "Attachment not found")
    safe_name = att.filename.replace('"', "")
    return Response(content=att.data, media_type=att.content_type, headers={
        "Content-Disposition": f'attachment; filename="{safe_name}"'})
