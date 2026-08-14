import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.serializers import project_out, project_summary, quote_out, request_out
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import require_admin
from app.core.security import new_api_token
from app.models import (
    ApiToken, CreditTransaction, KnowledgeBase, ModelEndpoint, Organization,
    Project, ProjectModelConfig, Quote, QuoteAttachment, Request, User, utcnow,
)
from app.schemas.schemas import (
    AppSettingsIn, CreditAdjustIn, ModelConfigIn, PriceIn, ProjectPatchIn,
    QuoteCancelIn, QuoteCreateIn, QuoteIn, QuotePatchIn, StatusIn,
)
from app.services import (
    app_settings, brand, dev_concurrency, egress, routines as routines_svc,
    speciality as speciality_svc, stripe_svc, vision,
)
from app.services.pricing import load_static
from app.services.lifecycle import TransitionError, transition_async
from app.services.statuses import STATUSES
from app.services.sysmsg import post_system_message
from app.workers.celery_app import celery

log = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # per file; stored in-DB (alpha scale)

router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])


async def _get_project(db: AsyncSession, project_id: str) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db)):
    projects = (await db.execute(
        select(Project, Organization.name)
        .join(Organization, Organization.id == Project.org_id)
        .order_by(Project.created_at.desc()))).all()
    counts = (await db.execute(
        select(Project.status, func.count()).group_by(Project.status))).all()
    return {
        "projects": [{**project_summary(p), "org_name": org_name, "org_id": p.org_id}
                     for p, org_name in projects],
        "counts": {"by_status": {status: n for status, n in counts}},
    }


async def _egress_out(db: AsyncSession) -> dict:
    """The egress fields for the settings payload."""
    stored = await app_settings.get_value(db, egress.ALLOWLIST_KEY, None)
    return {
        "egress_lockdown_enabled": await app_settings.get_flag(db, egress.ENABLED_KEY),
        "egress_allowlist": list(stored) if stored is not None else list(egress.DEFAULT_ALLOWLIST),
        # Enforced only on Kubernetes; the SPA shows this so the toggle isn't read
        # as a silent no-op on a compose instance.
        "egress_enforced_on": "kubernetes",
    }



async def _fees_out(db: AsyncSession) -> list[dict]:
    """§fees: the per-track engagement fee rows for the settings payload -
    the specialities.json default, the stored override, and what actually
    charges (speciality.effective_base_fee, the one resolver)."""
    overrides = await app_settings.get_value(db, speciality_svc.FEE_OVERRIDES_KEY) or {}
    rows = []
    for s in load_static("specialities.json")["specialities"]:
        if not s.get("enabled"):
            continue
        rows.append({
            "id": s["id"], "label": s["label"],
            "default_fee_credits": speciality_svc.clean_fee(s.get("base_fee_credits")) or 0.0,
            "override_credits": speciality_svc.clean_fee(overrides.get(s["id"])),
            "effective_fee_credits": speciality_svc.effective_base_fee(s, overrides),
        })
    return rows


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    """Runtime, admin-editable global settings: the deposit-pause flags, the
    instance-default model's image-support declaration (§chat images) and the
    dev-sandbox egress lockdown (§egress)."""
    out = await app_settings.get_deposit_pause(db)
    out["default_model_supports_images"] = await app_settings.get_flag(
        db, vision.DEFAULT_MODEL_IMAGES_KEY)
    out["routines_disabled"] = await app_settings.get_flag(
        db, routines_svc.ROUTINES_DISABLED)
    out["default_model"] = settings.openai_model
    out.update(await _egress_out(db))
    out["speciality_fees"] = await _fees_out(db)
    return out


@router.put("/settings")
async def update_settings(body: AppSettingsIn, db: AsyncSession = Depends(get_db)):
    await app_settings.set_deposit_pause(
        db, pause_ai=body.pause_ai_deposits, pause_direct=body.pause_direct_deposits,
        pause_auto_dev=body.pause_auto_dev_deposits, pause_chat=body.pause_chat_deposits,
    )
    if body.default_model_supports_images is not None:
        await app_settings.set_flag(db, vision.DEFAULT_MODEL_IMAGES_KEY,
                                    body.default_model_supports_images)
    if body.routines_disabled is not None:
        # §routines instance kill switch: read on every sweep tick and every
        # customer write, so flipping it takes effect without a deploy.
        await app_settings.set_flag(db, routines_svc.ROUTINES_DISABLED,
                                    body.routines_disabled)
    if body.egress_allowlist is not None:
        try:
            cleaned = egress.normalize_list(body.egress_allowlist)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid egress allowlist entry: {exc}")
        await app_settings.set_value(db, egress.ALLOWLIST_KEY, cleaned)
    if body.egress_lockdown_enabled is not None:
        await app_settings.set_flag(db, egress.ENABLED_KEY, body.egress_lockdown_enabled)
    if body.speciality_fee_overrides is not None:
        known = {s["id"] for s in load_static("specialities.json")["specialities"]}
        cleaned: dict[str, float] = {}
        for sid, fee in body.speciality_fee_overrides.items():
            if sid not in known:
                raise HTTPException(422, f"Unknown speciality: {sid}")
            if fee is None:
                continue  # cleared - the specialities.json default applies again
            val = speciality_svc.clean_fee(fee)
            if val is None:
                raise HTTPException(422, f"Invalid fee for {sid}: a non-negative number of credits")
            cleaned[sid] = val
        await app_settings.set_value(db, speciality_svc.FEE_OVERRIDES_KEY, cleaned)
    await db.commit()
    out = await app_settings.get_deposit_pause(db)
    out["default_model_supports_images"] = await app_settings.get_flag(
        db, vision.DEFAULT_MODEL_IMAGES_KEY)
    out["routines_disabled"] = await app_settings.get_flag(
        db, routines_svc.ROUTINES_DISABLED)
    out["default_model"] = settings.openai_model
    out.update(await _egress_out(db))
    out["speciality_fees"] = await _fees_out(db)
    return out


@router.post("/knowledge/reindex")
async def reindex_knowledge():
    """Force a full re-embed + Meilisearch re-index of the /knowledge KB now
    (bypassing the beat's change-detection). Async: dispatches ingest_knowledge."""
    celery.send_task("app.workers.tasks.ingest_knowledge", args=[True])
    return {"status": "dispatched"}


@router.post("/projects/{project_id}/status")
async def set_status(project_id: str, body: StatusIn, db: AsyncSession = Depends(get_db)):
    if body.status not in STATUSES:
        raise HTTPException(400, "Unknown status")
    project = await _get_project(db, project_id)
    try:
        await transition_async(db, project, body.status, "admin", body.note)
    except TransitionError as exc:
        raise HTTPException(409, str(exc))
    await db.commit()  # commit before enqueuing so the worker sees the new status
    # entering development kicks the dev pipeline (unless blocked); §parallel-
    # builds MR1: through the slot chokepoint - a build already in flight means
    # the status change stands alone (previously this dispatched unguarded).
    if body.status == "development" and not project.block_auto_development:
        try:
            run_id = await run_in_threadpool(dev_concurrency.acquire_for_project,
                                             project.id)
            celery.send_task("app.workers.tasks.run_development", args=[project.id],
                             kwargs={"run_id": run_id})
        except dev_concurrency.SlotRefused as exc:
            log.info("admin status kick skipped for %s: %s", project.id, exc)
    # §8: payment_due may auto-advance if the balance already covers the estimate
    if body.status == "payment_due":
        celery.send_task("app.workers.tasks.maybe_start_development", args=[project.id])
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.patch("/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatchIn,
                        db: AsyncSession = Depends(get_db)):
    project = await _get_project(db, project_id)
    if body.tier is not None:
        project.tier = body.tier
    if body.subdomain is not None:
        project.subdomain = body.subdomain
    if body.block_auto_development is not None:
        project.block_auto_development = body.block_auto_development
    # kb_ids: null is meaningful (reset to "all enabled KBs"), so only an
    # explicitly sent field is applied - never the pydantic default.
    if "kb_ids" in body.model_fields_set:
        if body.kb_ids is None:
            project.kb_ids = None
        else:
            known = set((await db.execute(select(KnowledgeBase.id))).scalars().all())
            unknown = sorted(set(body.kb_ids) - known)
            if unknown:
                raise HTTPException(422, f"Unknown knowledge base id(s): {', '.join(unknown)}")
            project.kb_ids = sorted(set(body.kb_ids))
    if "dev_max_iterations" in body.model_fields_set:
        project.dev_max_iterations = body.dev_max_iterations
    if "dev_parallel_limit" in body.model_fields_set:
        project.dev_parallel_limit = body.dev_parallel_limit
    if "dev_cpu_request" in body.model_fields_set:
        project.dev_cpu_request = body.dev_cpu_request
    if "dev_mem_request" in body.model_fields_set:
        project.dev_mem_request = body.dev_mem_request
    await db.commit()
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.get("/projects/{project_id}/model-config")
async def get_model_config(project_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(ProjectModelConfig).where(
        ProjectModelConfig.project_id == project_id))).scalar_one_or_none()
    return {"endpoint_id": row.endpoint_id if row else None}


@router.put("/projects/{project_id}/model-config")
async def set_model_config(project_id: str, body: ModelConfigIn,
                           db: AsyncSession = Depends(get_db)):
    await _get_project(db, project_id)
    row = (await db.execute(select(ProjectModelConfig).where(
        ProjectModelConfig.project_id == project_id))).scalar_one_or_none()
    # No endpoint chosen → clear the per-project override (fall back to the global
    # default) by dropping the row entirely.
    if not body.endpoint_id:
        if row is not None:
            await db.delete(row)
            await db.commit()
        return {"ok": True}
    endpoint = await db.get(ModelEndpoint, body.endpoint_id)
    if endpoint is None:
        raise HTTPException(404, "Model endpoint not found")
    if not endpoint.model_name:
        raise HTTPException(400, "This endpoint has no model set - edit it on the "
                                 "Model configuration page first")
    if row is None:
        row = ProjectModelConfig(project_id=project_id, endpoint_id=endpoint.id)
        db.add(row)
    else:
        row.endpoint_id = endpoint.id
        # The model + credentials now come from the endpoint; drop any legacy inline.
        row.model_name = None
        row.openai_base_url = None
        row.openai_api_key_enc = None
    await db.commit()
    return {"ok": True}


@router.post("/requests/{request_id}/price")
async def price_request(request_id: str, body: PriceIn, db: AsyncSession = Depends(get_db)):
    req = await db.get(Request, request_id)
    if req is None:
        raise HTTPException(404, "Request not found")
    req.price_credits = body.price_credits
    req.status = "quoted"
    await db.commit()
    return request_out(req)


@router.post("/requests/{request_id}/quote")
async def quote_request(request_id: str, body: QuoteIn, db: AsyncSession = Depends(get_db)):
    req = await db.get(Request, request_id)
    if req is None:
        raise HTTPException(404, "Request not found")
    quote = Quote(project_id=req.project_id, request_id=req.id, amount=body.amount)
    db.add(quote)
    await db.flush()
    try:
        link = stripe_svc.create_quote_payment_link(
            quote.id, body.amount, f"{settings.brand_name} quote - {req.title}")
        quote.stripe_payment_link = link
        quote.status = "sent"
    except stripe_svc.StripeUnavailable:
        quote.status = "draft"
    req.status = "quoted"
    await db.commit()
    return {"id": quote.id, "request_id": req.id, "amount": quote.amount,
            "currency": quote.currency, "status": quote.status,
            "payment_link": quote.stripe_payment_link}


@router.post("/projects/{project_id}/quote")
async def quote_project(project_id: str, body: QuoteIn,
                        db: AsyncSession = Depends(get_db)):
    """Project-level quote (used for direct-quote projects). Creates a Stripe
    payment link and posts it to the project chat."""
    project = await _get_project(db, project_id)
    quote = Quote(project_id=project.id, request_id=None, amount=body.amount)
    db.add(quote)
    await db.flush()
    try:
        link = stripe_svc.create_quote_payment_link(
            quote.id, body.amount, f"{settings.brand_name} quote - {project.name}")
        quote.stripe_payment_link = link
        quote.status = "sent"
    except stripe_svc.StripeUnavailable:
        quote.status = "draft"
    await db.commit()
    return {"id": quote.id, "project_id": project.id, "amount": quote.amount,
            "currency": quote.currency, "status": quote.status,
            "payment_link": quote.stripe_payment_link}


async def _get_quote(db: AsyncSession, quote_id: str) -> Quote:
    quote = (await db.execute(select(Quote).where(Quote.id == quote_id)
                              .options(selectinload(Quote.attachments)))).scalar_one_or_none()
    if quote is None:
        raise HTTPException(404, "Quote not found")
    return quote


@router.post("/projects/{project_id}/quotes", status_code=201)
async def create_credit_quote(project_id: str, body: QuoteCreateIn,
                              db: AsyncSession = Depends(get_db)):
    """Credit-priced quote for the Quotes tab: the customer accepts (charged
    from the org wallet, commits the consultant to deliver to the customer repo) or
    denies it. Distinct from the Stripe payment-link quotes above."""
    project = await _get_project(db, project_id)
    quote = Quote(project_id=project.id, title=body.title, details=body.details,
                  amount=body.price_credits, currency="credits",
                  price_credits=body.price_credits, status="sent")
    db.add(quote)
    await db.flush()
    await post_system_message(
        db, project.id,
        f"{settings.consultant_first_name} sent a new quote: {quote.title} ({quote.price_credits:g} credits). "
        f"Review it in the Quotes tab.")
    await db.commit()
    await db.refresh(quote, ["attachments"])
    return quote_out(quote)


@router.patch("/quotes/{quote_id}")
async def patch_quote(quote_id: str, body: QuotePatchIn,
                      db: AsyncSession = Depends(get_db)):
    """Title/details are editable at any time; the price locks once decided."""
    quote = await _get_quote(db, quote_id)
    if body.title is not None:
        quote.title = body.title
    if body.details is not None:
        quote.details = body.details
    if body.price_credits is not None:
        if quote.status in ("accepted", "denied", "canceled"):
            raise HTTPException(409, "The price can no longer change once the quote is decided")
        quote.price_credits = body.price_credits
        quote.amount = body.price_credits
    await db.commit()
    return quote_out(quote)


@router.post("/quotes/{quote_id}/cancel")
async def cancel_quote(quote_id: str, body: QuoteCancelIn,
                       db: AsyncSession = Depends(get_db)):
    """Withdraw a credit quote the admin won't deliver. Terminal like a deny;
    when the quote had been accepted (customer already charged), the admin
    chooses how much of the paid credits to refund (0 up to the full price)."""
    quote = await _get_quote(db, quote_id)
    if quote.price_credits is None:
        raise HTTPException(409, "Only credit quotes can be canceled here")
    if quote.status not in ("sent", "accepted"):
        raise HTTPException(409, f"Quote is {quote.status}; only a sent or accepted "
                                 "quote can be canceled")
    was_accepted = quote.status == "accepted"
    refund = body.refund_credits or 0.0
    if refund and not was_accepted:
        raise HTTPException(409, "Nothing to refund: the quote was never paid")
    if refund > quote.price_credits:
        raise HTTPException(400, f"Refund must be between 0 and {quote.price_credits:g} credits")
    project = await db.get(Project, quote.project_id)
    if refund:
        org = await db.get(Organization, project.org_id)
        org.credit_balance = (org.credit_balance or 0.0) + refund
        db.add(CreditTransaction(org_id=org.id, project_id=project.id, amount=refund,
                                 kind="refund", detail=f"Quote canceled: {quote.title}"))
    quote.status = "canceled"
    quote.decision_comment = body.comment
    quote.decided_at = utcnow()
    quote.refunded_credits = refund if was_accepted else None
    note = f"Quote canceled by {settings.consultant_first_name}: {quote.title}"
    if was_accepted:
        note += f" - {refund:g} of {quote.price_credits:g} paid credits refunded"
    if body.comment:
        note += f" - {body.comment}"
    await post_system_message(db, project.id, note)
    await db.commit()
    owner = (await db.execute(select(User).where(User.org_id == project.org_id)
                              .order_by(User.created_at))).scalars().first()
    if owner:
        celery.send_task("app.workers.tasks.send_email", args=[
            owner.email, brand.subject(f"Quote canceled: {quote.title}"),
            note + f"\n{settings.app_base_url}/projects/{project.id}"])
    return quote_out(quote)


@router.post("/quotes/{quote_id}/attachments", status_code=201)
async def upload_quote_attachments(quote_id: str, files: list[UploadFile],
                                   db: AsyncSession = Depends(get_db)):
    quote = await _get_quote(db, quote_id)
    if not files:
        raise HTTPException(400, "No files provided")
    added = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"{f.filename}: attachments are limited to "
                                     f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        att = QuoteAttachment(quote_id=quote.id, filename=f.filename or "attachment",
                              content_type=f.content_type or "application/octet-stream",
                              size_bytes=len(data), data=data)
        db.add(att)
        added.append(att)
    await db.flush()
    payload = [{"id": a.id, "filename": a.filename, "content_type": a.content_type,
                "size_bytes": a.size_bytes} for a in added]
    await db.commit()
    return payload


@router.delete("/quotes/{quote_id}/attachments/{attachment_id}")
async def delete_quote_attachment(quote_id: str, attachment_id: str,
                                  db: AsyncSession = Depends(get_db)):
    quote = await _get_quote(db, quote_id)
    att = next((a for a in quote.attachments if a.id == attachment_id), None)
    if att is None:
        raise HTTPException(404, "Attachment not found")
    await db.delete(att)
    await db.commit()
    return {"ok": True}


@router.get("/projects/{project_id}/spend")
async def project_spend(project_id: str, db: AsyncSession = Depends(get_db)):
    """What the customer has spent on this project: net project-scoped credit
    movements (consumption, review fees, minus refunds) plus quotes paid via
    Stripe. Credits and EUR are summed at parity for the alpha (pricing.py)."""
    await _get_project(db, project_id)
    rows = (await db.execute(
        select(CreditTransaction.kind, func.sum(CreditTransaction.amount))
        .where(CreditTransaction.project_id == project_id)
        .group_by(CreditTransaction.kind))).all()
    by_kind = {kind: round(total, 4) for kind, total in rows}
    credits_spent = round(-sum(by_kind.values()), 4)  # debits are negative
    quotes_paid = (await db.execute(
        select(func.coalesce(func.sum(Quote.amount), 0.0))
        .where(Quote.project_id == project_id, Quote.status == "paid"))).scalar_one()
    return {
        "credits_spent": credits_spent,
        "by_kind": by_kind,
        "quotes_paid": round(quotes_paid, 4),
        "total_spent": round(credits_spent + quotes_paid, 4),
    }


@router.post("/projects/{project_id}/refund-review")
async def refund_review(project_id: str, db: AsyncSession = Depends(get_db)):
    """Refund a customer's review-request fee for this project (§ review charge)."""
    project = await _get_project(db, project_id)
    charged = (await db.execute(select(CreditTransaction).where(
        CreditTransaction.project_id == project.id,
        CreditTransaction.kind == "review_request").order_by(
        CreditTransaction.created_at.desc()))).scalars().first()
    if charged is None:
        raise HTTPException(404, "No review fee to refund on this project")
    already = (await db.execute(select(CreditTransaction).where(
        CreditTransaction.project_id == project.id,
        CreditTransaction.kind == "refund"))).scalars().first()
    if already is not None:
        raise HTTPException(409, "Review fee already refunded")
    org = await db.get(Organization, project.org_id)
    amount = -charged.amount  # charged is negative
    org.credit_balance = (org.credit_balance or 0.0) + amount
    db.add(CreditTransaction(org_id=org.id, project_id=project.id, amount=amount,
                             kind="refund", detail="Review-request fee refunded"))
    await db.commit()
    return {"credit_balance": round(org.credit_balance, 4), "refunded": amount}


@router.post("/orgs/{org_id}/credits")
async def adjust_credits(org_id: str, body: CreditAdjustIn,
                         db: AsyncSession = Depends(get_db)):
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "Organization not found")
    org.credit_balance = (org.credit_balance or 0.0) + body.amount
    db.add(CreditTransaction(org_id=org.id, amount=body.amount, kind="adjustment",
                             detail=body.reason))
    await db.commit()
    pending = (await db.execute(select(Project.id).where(
        Project.org_id == org.id, Project.status == "payment_due"))).all()
    for (pid,) in pending:
        celery.send_task("app.workers.tasks.maybe_start_development", args=[pid])
    return {"credit_balance": round(org.credit_balance, 4)}


@router.post("/hub-token", status_code=201)
async def mint_hub_token(admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    """Mint a hub-scoped API token authenticating a central Scalevisor Hub to the
    /api/hub control surface. Returned in plaintext once, like a user token; hub
    tokens can read usage and grant credits but never bill a knowledge query."""
    plaintext, token_hash = new_api_token()
    row = ApiToken(user_id=admin.id, token_hash=token_hash, name="hub", scope="hub")
    db.add(row)
    await db.commit()
    return {"id": row.id, "name": row.name, "scope": row.scope, "token": plaintext}


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(User, Organization).join(Organization, Organization.id == User.org_id)
        .order_by(User.created_at.desc()))).all()
    return [{"id": u.id, "email": u.email, "role": u.role, "org_id": o.id,
             "org_name": o.name, "credit_balance": round(o.credit_balance or 0.0, 4),
             "email_verified": u.email_verified, "created_at": u.created_at}
            for u, o in rows]
