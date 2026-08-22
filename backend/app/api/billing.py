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
from app.services import brand, stripe_svc
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
             "kind": t.kind, "detail": t.detail, "created_at": t.created_at} for t in rows]


@router.post("/topup")
async def topup(body: TopupIn, user: User = Depends(get_current_user)):
    try:
        url = stripe_svc.create_topup_checkout(user.org_id, body.amount, user.email)
    except stripe_svc.TopupTooSmall as exc:
        raise HTTPException(400, str(exc))
    except stripe_svc.StripeUnavailable:
        raise HTTPException(503, "Stripe is not configured on this deployment")
    return {"checkout_url": url}


@router.post("/stripe/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe_svc.parse_webhook(payload, sig)
    except Exception:
        raise HTTPException(400, "Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        obj = event["data"]["object"]
        meta = obj.get("metadata", {})
        if meta.get("kind") == "topup":
            org = await db.get(Organization, meta["org_id"])
            if org is None:
                # Several dev instances can listen on the same Stripe sandbox;
                # an event for another instance's org is not ours to credit.
                log.warning("Ignoring topup webhook for unknown org %s", meta["org_id"])
                return {"ok": True}
            amount = float(meta["amount"])
            org.credit_balance = (org.credit_balance or 0.0) + amount
            db.add(CreditTransaction(org_id=org.id, amount=amount, kind="topup",
                                     stripe_ref=obj["id"]))
            await db.commit()
            from app.workers.celery_app import celery
            pending = (await db.execute(select(Project.id).where(
                Project.org_id == org.id, Project.status == "payment_due"))).all()
            for (pid,) in pending:
                celery.send_task("app.workers.tasks.maybe_start_development", args=[pid])
        elif meta.get("kind") == "quote":
            quote = await db.get(Quote, meta["quote_id"])
            if quote:
                quote.status = "paid"
                await db.commit()
    return {"ok": True}


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
