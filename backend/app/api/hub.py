"""Hub control surface (PROMPT hub link). Every endpoint is authenticated with a
hub-scoped API token (require_hub_token) and mounted without CSRF like the
knowledge router. A central Scalevisor Hub reads this spoke's identity/usage and
pushes credit grants here; with no hub configured the surface simply goes unused.
Money paths never read-modify-write a balance: grants apply an atomic UPDATE and
are idempotent on the hub-supplied key, so an at-least-once hub never double-credits.
Privacy boundary: per-org data (usage, credit events) is scoped to HUB-RELEVANT
orgs only - orgs the hub created (hub_managed) or has funded (a HubCreditGrant
row) - so a hub token can never read the spoke's direct-customer business.
Every endpoint is rate-limited per hub token as the runaway/stolen-token backstop."""
import logging
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import message_out, project_out
from app.core.config import settings
from app.core.db import SyncSession, get_db
from app.core.deps import rate_limit, require_hub_token
from app.core.encryption import encrypt
from app.version import get_version
from app.models import (
    CreditTransaction, HubCreditGrant, Message, Organization, Project, StatusChange,
    User, utcnow,
)
from app.schemas.schemas import (
    HubEvalIn, HubGrantIn, HubOrgCreateIn, HubProjectActionIn, HubProjectCreateIn,
    HubProjectMessageIn, HubRequestIn, MemoryIn,
)
from app.services import app_settings, devfeed, kb_audit, naming, project_actions, speciality as speciality_svc, sshkeys
from app.services.pricing import load_static
from app.workers.celery_app import celery

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hub", tags=["hub"],
                   dependencies=[Depends(require_hub_token)])

# Max credit-event rows returned in one page (the hub advances `since` to paginate).
EVENT_BATCH = 500


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    try:
        dt = datetime.fromisoformat(since)
    except ValueError:
        raise HTTPException(400, "since must be an ISO-8601 timestamp")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hub_org_ids():
    """Subquery of org ids the hub may see per-org data for: orgs it created
    (hub_managed) or has ever funded (grant history). Everything else is the
    spoke owner's direct business and stays invisible to a hub token."""
    return select(Organization.id).where(or_(
        Organization.hub_managed.is_(True),
        Organization.id.in_(select(HubCreditGrant.org_id)),
    )).scalar_subquery()


async def _hub_read_limit(request: Request, user: User) -> None:
    """Shared read budget across the polling endpoints (info/usage/events)."""
    await rate_limit(request, "hub-read", 240, 60, identity=f"hub:{user.id}")


@router.get("/info")
async def info(request: Request, db: AsyncSession = Depends(get_db),
               user: User = Depends(require_hub_token)):
    """Brand-agnostic spoke identity for hub registration. Counts are spoke-wide
    aggregates (no per-org detail leaks through a count)."""
    await _hub_read_limit(request, user)
    orgs = (await db.execute(select(func.count()).select_from(Organization))).scalar_one()
    projects = (await db.execute(select(func.count()).select_from(Project))).scalar_one()
    specialities = load_static("specialities.json")["specialities"]
    fee_overrides = await app_settings.get_value(db, speciality_svc.FEE_OVERRIDES_KEY)
    return {
        "deploy_domain": settings.deploy_domain,
        "credit_currency": settings.credit_currency,
        "org_count": orgs,
        "project_count": projects,
        "version": get_version(),
        # What this instance offers the network, and its billable speciality
        # catalog with each track's base engagement fee - so the hub reads both
        # over MCP (spoke_info) instead of scraping the public settings page.
        "capabilities": settings.capabilities_list,
        "specialities": [
            {"id": s["id"], "label": s["label"],
             "description": s.get("description", ""),
             "base_fee_credits": speciality_svc.effective_base_fee(s, fee_overrides)}
            for s in specialities if s.get("enabled")
        ],
    }


@router.get("/usage")
async def usage(request: Request, since: str | None = None,
                db: AsyncSession = Depends(get_db),
                user: User = Depends(require_hub_token)):
    """Credit-transaction rollup grouped by kind over HUB-RELEVANT orgs only,
    optionally since a cursor. `cursor` is the newest created_at seen, to advance
    on the next poll."""
    await _hub_read_limit(request, user)
    cutoff = _parse_since(since)
    q = select(CreditTransaction.kind, func.sum(CreditTransaction.amount),
               func.max(CreditTransaction.created_at)).where(
        CreditTransaction.org_id.in_(hub_org_ids()))
    if cutoff is not None:
        q = q.where(CreditTransaction.created_at > cutoff)
    rows = (await db.execute(q.group_by(CreditTransaction.kind))).all()
    by_kind = {kind: round(total or 0.0, 4) for kind, total, _ in rows}
    cursor = max((mx for _, _, mx in rows if mx is not None), default=None)
    return {
        "by_kind": by_kind,
        "net": round(sum(by_kind.values()), 4),
        "cursor": cursor.isoformat() if cursor else None,
    }


@router.get("/credit-events")
async def credit_events(request: Request, since: str | None = None,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_hub_token)):
    """Raw credit-transaction feed over HUB-RELEVANT orgs only, after `since`,
    oldest first, capped per page. `cursor` is the newest created_at returned
    (poll again with since=cursor)."""
    await _hub_read_limit(request, user)
    cutoff = _parse_since(since)
    q = (select(CreditTransaction)
         .where(CreditTransaction.org_id.in_(hub_org_ids()))
         .order_by(CreditTransaction.created_at))
    if cutoff is not None:
        q = q.where(CreditTransaction.created_at > cutoff)
    rows = (await db.execute(q.limit(EVENT_BATCH))).scalars().all()
    events = [{"id": t.id, "org_id": t.org_id, "project_id": t.project_id,
               "amount": round(t.amount, 4), "kind": t.kind, "detail": t.detail,
               "created_at": t.created_at.isoformat()} for t in rows]
    cursor = rows[-1].created_at.isoformat() if rows else (since or None)
    return {"events": events, "cursor": cursor}


@router.get("/orgs/find")
async def find_org(email: str, request: Request, db: AsyncSession = Depends(get_db),
                   user: User = Depends(require_hub_token)):
    """Resolve a customer email to its org (the hub keys grants by org_id). Only
    for linking an EXISTING direct customer who asked the hub to fund them -
    brokered hub customers use POST /orgs instead. Tightly rate-limited: exact
    match only, so enumeration costs one 404 per guess."""
    await rate_limit(request, "hub-find", 30, 600, identity=f"hub:{user.id}")
    found = (await db.execute(select(User).where(
        func.lower(User.email) == email.strip().lower()))).scalar_one_or_none()
    if found is None:
        raise HTTPException(404, "No user with that email")
    org = await db.get(Organization, found.org_id)
    return {"org_id": org.id, "org_name": org.name}


@router.post("/orgs", status_code=201)
async def create_org(body: HubOrgCreateIn, request: Request,
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_hub_token)):
    """Create a brokered, hub-managed org for one of the HUB's customers (§hub
    pass-through). No spoke User is created - the customer never logs in here;
    the hub drives everything on their behalf. Idempotent on idempotency_key:
    a replayed create returns the existing org and writes nothing."""
    await rate_limit(request, "hub-org-create", 30, 600, identity=f"hub:{user.id}")
    org = Organization(name=body.name.strip(), hub_managed=True,
                       hub_create_key=body.idempotency_key)
    db.add(org)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(select(Organization).where(
            Organization.hub_create_key == body.idempotency_key))).scalar_one()
        return {"already_created": True, "org_id": existing.id,
                "org_name": existing.name,
                "credit_balance": round(existing.credit_balance or 0.0, 4)}
    await db.commit()
    return {"already_created": False, "org_id": org.id, "org_name": org.name,
            "credit_balance": 0.0}


@router.post("/credits/grant")
async def grant_credits(body: HubGrantIn, request: Request,
                        org_id: str | None = None,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_hub_token)):
    """Push credits into an org wallet, idempotent on idempotency_key. A replayed
    key returns the original grant (already_applied: true) and writes nothing; a
    fresh key applies an atomic balance UPDATE + a hub_grant ledger row, then
    re-checks any payment_due projects the top-up may now cover. Backstops
    against a stolen hub token: per-token rate limit + a rolling-24h total
    ceiling (hub_max_daily_grant_credits, 0 disables)."""
    await rate_limit(request, "hub-grant", 60, 600, identity=f"hub:{user.id}")
    if not (0 < body.amount <= settings.hub_max_grant_credits):
        raise HTTPException(
            400, f"amount must be > 0 and <= {settings.hub_max_grant_credits:g} credits")
    if settings.hub_max_daily_grant_credits > 0:
        # Ceiling check-then-insert has a small concurrent-grant race; it is a
        # backstop against runaway minting, not the accounting ledger.
        day_total = (await db.execute(select(func.coalesce(
            func.sum(HubCreditGrant.amount), 0.0)).where(
            HubCreditGrant.created_at > utcnow() - timedelta(hours=24)))).scalar_one()
        if day_total + body.amount > settings.hub_max_daily_grant_credits:
            raise HTTPException(
                429, f"daily grant ceiling reached "
                     f"({settings.hub_max_daily_grant_credits:g} credits/24h)")
    # org_id rides as a query param from the MCP sidecar (HUB_QUERY_KEYS), but a
    # direct caller may put it in the body; accept either, preferring the query.
    target_org_id = org_id or body.org_id
    if not target_org_id:
        raise HTTPException(422, "org_id is required (query param or body)")
    org = await db.get(Organization, target_org_id)
    if org is None:
        raise HTTPException(404, "Organization not found")

    grant = HubCreditGrant(idempotency_key=body.idempotency_key, org_id=org.id,
                           amount=body.amount, detail=body.detail)
    db.add(grant)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(select(HubCreditGrant).where(
            HubCreditGrant.idempotency_key == body.idempotency_key))).scalar_one()
        current = await db.get(Organization, existing.org_id)
        return {"already_applied": True, "grant_id": existing.id,
                "org_id": existing.org_id, "amount": existing.amount,
                "credit_balance": round(current.credit_balance or 0.0, 4)}

    # Atomic balance update - never the read-modify-write pattern used elsewhere.
    await db.execute(update(Organization).where(Organization.id == org.id)
                     .values(credit_balance=Organization.credit_balance + body.amount))
    db.add(CreditTransaction(org_id=org.id, project_id=None, amount=body.amount,
                             kind="hub_grant", detail=body.detail))
    await db.commit()

    pending = (await db.execute(select(Project.id).where(
        Project.org_id == org.id, Project.status == "payment_due"))).all()
    for (pid,) in pending:
        celery.send_task("app.workers.tasks.maybe_start_development", args=[pid])

    await db.refresh(org)
    return {"already_applied": False, "grant_id": grant.id, "org_id": org.id,
            "amount": body.amount, "credit_balance": round(org.credit_balance or 0.0, 4)}


def _run_kb_audit() -> dict:
    with SyncSession() as db:
        return kb_audit.run_audit(db)


@router.post("/kb-audit")
async def kb_audit_report(request: Request, user: User = Depends(require_hub_token)):
    """Self-audit the KB confidentiality boundary and return a report-only risk
    assessment (score + per-probe flags). Raw KB text, retrieved chunks and
    synthesized answers never leave the spoke - sending them would be the leak.
    Bills nothing but is compute-heavy, so rate-limited per hub token. The sync
    RAG/LLM battery runs in a threadpool so it never blocks the event loop;
    kb_audit.run_audit is bounded well under 120s and returns level 'unknown'
    rather than raising if the stack is unavailable."""
    await rate_limit(request, "hub-audit", 6, 600, identity=f"hub:{user.id}")
    return await run_in_threadpool(_run_kb_audit)


def _run_eval(questions: list[str], k: int) -> dict:
    answers: list[dict] = []
    with SyncSession() as db:
        for q in questions:
            try:
                text, matched = kb_audit.run_unbilled_query(db, q, k)
            except Exception as exc:  # one question's retrieval/synthesis failure
                log.warning("hub eval question failed: %s", exc)
                answers.append({"question": q, "answer": "", "matched": False,
                                "error": "unavailable"})
                continue
            answers.append({"question": q, "answer": text, "matched": matched})
    return {"answers": answers, "count": len(answers)}


@router.post("/eval")
async def eval_questions(body: HubEvalIn, request: Request,
                         user: User = Depends(require_hub_token)):
    """Answer each eval question through this spoke's knowledge stack, UNBILLED,
    and return the guarded answer texts. Owner-consented (the spoke owner triggers
    the evals from the hub); every answer passes the same verbatim guard as
    search_knowledge, so no raw KB text can leak. Rate-limited per hub token
    (10/600s) so a runaway hub - each call is up to 4x2 LLM calls - can't burn the
    owner's LLM budget. The sync RAG/LLM battery runs in a threadpool; a single
    question's failure soft-fails that answer without failing the batch."""
    await rate_limit(request, "hub-eval", 10, 600, identity=f"hub:{user.id}")
    return await run_in_threadpool(_run_eval, body.questions, body.k)


# ---- from-hub projects (§pass-through P1) ----

async def _hub_project(db: AsyncSession, project_id: str,
                       org_id: str | None = None) -> Project:
    """Load a project for the hub surface. 404 unless it exists AND is
    source='hub' - the hard boundary: a hub token can never read or act on the
    spoke owner's direct-customer projects. The optional expected `org_id`
    (derived server-side by the hub from its own org link, never client-supplied
    there) turns a hub-BFF authz bug into a 403 instead of a cross-customer leak."""
    project = await db.get(Project, project_id)
    if project is None or project.source != "hub":
        raise HTTPException(404, "Project not found")
    if org_id is not None and project.org_id != org_id:
        raise HTTPException(403, "Project does not belong to that org")
    return project


@router.post("/projects", status_code=201)
async def create_hub_project(body: HubProjectCreateIn, request: Request,
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_hub_token)):
    """Create a from-hub project in a brokered org, mirroring the customer create
    path (name bootstrap, SSH key, subdomain, workspace, demo creds, provisioning)
    with source='hub'. Never-trust-client: the deposit-pause gate re-runs here
    exactly as it does for customers. The subdomain is OPAQUE (uuid prefix only,
    never name-derived) so an anonymous customer's company name can't leak into
    public DNS/SNI. Provisioning passes no email - no spoke GitLab user is ever
    created for a hub customer."""
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    org = await db.get(Organization, body.spoke_org_id)
    if org is None:
        raise HTTPException(404, "Organization not found")
    if not org.hub_managed:
        raise HTTPException(403, "Projects can only be created in hub-managed (brokered) orgs")
    # Soft credit limit: a run may overdraw the wallet, but an OUTSTANDING (negative)
    # balance from earlier work must be settled before a new project is started -
    # the same principle that already blocks new requests at balance <= 0, extended
    # to new projects. Covers both the customer and admin from-hub create paths.
    if (org.credit_balance or 0.0) < -0.01:
        raise HTTPException(
            402, f"This account has an unpaid balance of {abs(org.credit_balance):.0f} "
                 "credits from earlier work. Settle it (top up) before starting a new project.")
    flags = await app_settings.get_deposit_pause(db)
    if app_settings.is_kind_paused(flags, body.kind):
        raise HTTPException(403, "deposits_paused")
    specs = {s["id"]: s for s in load_static("specialities.json")["specialities"] if s.get("enabled")}
    if body.kind == "ai":
        if body.speciality not in specs:
            raise HTTPException(400, "Unknown speciality")
    elif body.speciality is not None and body.speciality not in specs:
        raise HTTPException(400, "Unknown speciality")
    if not body.from_scratch:
        raise HTTPException(400, "Connected repositories are not supported for hub projects yet")

    project = Project(
        org_id=org.id, name=naming.name_from_description(body.description),
        kind=body.kind, speciality=body.speciality,
        description=body.description, from_scratch=True,
        sovereign=body.sovereign, sovereign_comment=body.sovereign_comment,
        block_auto_development=(body.kind in ("direct_quote", "chat")),
        source="hub", hub_ref=body.hub_ref,
    )
    if body.kind == "ai":
        private_pem, public_line = sshkeys.generate_keypair(f"{settings.brand_name} project")
        project.ssh_private_key_enc = encrypt(private_pem)
        project.ssh_public_key = public_line
    db.add(project)
    await db.flush()
    if body.kind == "ai":
        project.subdomain = naming.subdomain_for(project.id, "project")
        project.workspace_path = f"{settings.workspaces_dir}/{project.id}"
        project.demo_basic_auth_user = "demo"
        project.demo_basic_auth_pass_enc = encrypt(_secrets.token_urlsafe(12))
        # §threads Request #0: same contract as the customer create path.
        await project_actions.create_mvp_request(db, project)
    if body.kind == "chat":
        # §chat kind: same immediate-start contract as the customer route - the
        # opening fee is debited in the create transaction and the chat is live in
        # `development` right away (no evaluation/estimate/payment_due).
        try:
            await project_actions.charge_chat_upfront(db, project)
        except project_actions.ActionError as exc:
            raise HTTPException(exc.status, exc.detail)
        project.status = "development"
        db.add(StatusChange(project_id=project.id, from_status=None, to_status="development",
                            actor="customer", reason="Chat opened via hub"))
    else:
        db.add(StatusChange(project_id=project.id, from_status=None, to_status="draft",
                            actor="customer", reason="Project created via hub"))
    await db.commit()
    await db.refresh(project, ["repos"])
    if body.kind == "ai":
        celery.send_task("app.workers.tasks.provision_project", args=[project.id, ""])
    elif body.kind == "chat":
        # The description IS the opening message (same seed as the customer route).
        try:
            await project_actions.post_chat_message(db, project, "customer", "main",
                                                    body.description)
        except project_actions.ActionError as exc:
            raise HTTPException(exc.status, exc.detail)
    return project_out(project)


@router.get("/projects/{project_id}")
async def get_hub_project(project_id: str, request: Request, org_id: str | None = None,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_hub_token)):
    """Full project view (project_out shape, demo creds included - the hub UI
    shows them to the customer exactly like the spoke SPA does)."""
    await _hub_read_limit(request, user)
    project = await _hub_project(db, project_id, org_id)
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.get("/projects/{project_id}/evaluation")
async def get_hub_project_evaluation(project_id: str, request: Request,
                                     org_id: str | None = None,
                                     db: AsyncSession = Depends(get_db),
                                     user: User = Depends(require_hub_token)):
    await _hub_read_limit(request, user)
    project = await _hub_project(db, project_id, org_id)
    return project.evaluation or {"state": "none"}


@router.get("/projects/{project_id}/messages")
async def list_hub_project_messages(project_id: str, request: Request,
                                    thread: str = "main", org_id: str | None = None,
                                    db: AsyncSession = Depends(get_db),
                                    user: User = Depends(require_hub_token)):
    """Same contract as the customer GET /messages (thread param, full ordered
    list) so the shared UI's ProjectApi stays single-shaped."""
    await _hub_read_limit(request, user)
    project = await _hub_project(db, project_id, org_id)
    if not await project_actions.valid_thread(db, project, thread):
        raise HTTPException(404, "Unknown thread")
    rows = (await db.execute(select(Message).where(
        Message.project_id == project.id, Message.thread == thread)
        .order_by(Message.created_at))).scalars().all()
    return [message_out(m) for m in rows]


@router.get("/projects/{project_id}/dev-activity")
async def hub_project_dev_activity(project_id: str, request: Request,
                                   offset: int = 0, org_id: str | None = None,
                                   db: AsyncSession = Depends(get_db),
                                   user: User = Depends(require_hub_token)):
    """Live build console (§14.8), same offset-poll contract as the customer
    endpoint - devfeed redacts platform secrets before anything leaves."""
    await _hub_read_limit(request, user)
    project = await _hub_project(db, project_id, org_id)
    return await run_in_threadpool(devfeed.read_chunk, project, offset)


@router.post("/projects/{project_id}/actions")
async def hub_project_action(project_id: str, body: HubProjectActionIn,
                             request: Request, org_id: str | None = None,
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_hub_token)):
    """Run one customer-actor action, wrapping the SAME services/project_actions
    functions the SPA routes use - guards can't drift between the two surfaces."""
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    project = await _hub_project(db, project_id, org_id)
    try:
        if body.action == "evaluate":
            task_id = await project_actions.evaluate(db, project)
            return {"ok": True, "task_id": task_id}
        if body.action == "submit":
            await project_actions.submit(db, project)
        elif body.action == "approve-delivery":
            await project_actions.approve_delivery(db, project, "customer")
        elif body.action == "retry-build":
            await project_actions.retry_build(db, project, is_admin=False)
        elif body.action == "stop-build":
            project_actions.stop_build(project)
        elif body.action == "require-admin":
            await project_actions.require_review(db, project)
        elif body.action == "demo-start":
            project_actions.start_demo(project)
        elif body.action == "demo-stop":
            project_actions.stop_demo(project)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    await db.refresh(project, ["repos"])
    return {"ok": True, "project": project_out(project)}


# ---- from-hub project interactivity (§pass-through P2) ----

@router.post("/projects/{project_id}/messages", status_code=201)
async def post_hub_project_message(project_id: str, body: HubProjectMessageIn,
                                   request: Request, org_id: str | None = None,
                                   db: AsyncSession = Depends(get_db),
                                   user: User = Depends(require_hub_token)):
    """Post a chat message as the customer (the hub relays their words verbatim;
    the §12 classifier and email side effects run exactly as for the SPA)."""
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    project = await _hub_project(db, project_id, org_id)
    try:
        msg = await project_actions.post_chat_message(
            db, project, "customer", body.thread, body.body)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return message_out(msg)


@router.get("/projects/{project_id}/requests")
async def list_hub_project_requests(project_id: str, request: Request,
                                    org_id: str | None = None,
                                    db: AsyncSession = Depends(get_db),
                                    user: User = Depends(require_hub_token)):
    from app.api.serializers import request_out
    from app.models import Request as RequestRow
    await _hub_read_limit(request, user)
    project = await _hub_project(db, project_id, org_id)
    rows = (await db.execute(select(RequestRow).where(
        RequestRow.project_id == project.id)
        .order_by(RequestRow.created_at.desc()))).scalars().all()
    return [request_out(r) for r in rows]


@router.post("/projects/{project_id}/requests", status_code=201)
async def create_hub_project_request(project_id: str, body: HubRequestIn,
                                     request: Request, org_id: str | None = None,
                                     db: AsyncSession = Depends(get_db),
                                     user: User = Depends(require_hub_token)):
    from app.api.serializers import request_out
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    project = await _hub_project(db, project_id, org_id)
    try:
        req, _ = await project_actions.create_request(
            db, project, "customer", body.type, body.handling, body.body,
            repo_id=getattr(body, "repo_id", None))
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return request_out(req)


@router.post("/projects/{project_id}/requests/{request_id}/start")
async def start_hub_project_request(project_id: str, request_id: str,
                                    request: Request, org_id: str | None = None,
                                    db: AsyncSession = Depends(get_db),
                                    user: User = Depends(require_hub_token)):
    from app.api.serializers import request_out
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    project = await _hub_project(db, project_id, org_id)
    try:
        req = await project_actions.start_request(db, project, request_id)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return request_out(req)


@router.post("/projects/{project_id}/requests/{request_id}/cancel")
async def cancel_hub_project_request(project_id: str, request_id: str,
                                     request: Request, org_id: str | None = None,
                                     db: AsyncSession = Depends(get_db),
                                     user: User = Depends(require_hub_token)):
    """§requests cancel pass-through - the hub always acts as the customer."""
    from app.api.serializers import request_out
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    project = await _hub_project(db, project_id, org_id)
    try:
        req = await project_actions.cancel_request(db, project, "customer", request_id)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return request_out(req)


@router.post("/projects/{project_id}/requests/{request_id}/validate")
async def validate_hub_project_request(project_id: str, request_id: str,
                                       request: Request, org_id: str | None = None,
                                       db: AsyncSession = Depends(get_db),
                                       user: User = Depends(require_hub_token)):
    """§requests validate pass-through - the hub always acts as the customer."""
    from app.api.serializers import request_out
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    project = await _hub_project(db, project_id, org_id)
    try:
        req = await project_actions.validate_request(db, project, "customer", request_id)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return request_out(req)


@router.get("/projects/{project_id}/memory")
async def list_hub_project_memory(project_id: str, request: Request,
                                  org_id: str | None = None,
                                  db: AsyncSession = Depends(get_db),
                                  user: User = Depends(require_hub_token)):
    """Project Memory passthrough (values decrypt in the response exactly as the
    customer API serves them - the hub relays, never stores)."""
    from app.api.memory import _entry_out
    from app.models import ProjectMemory
    await _hub_read_limit(request, user)
    project = await _hub_project(db, project_id, org_id)
    rows = (await db.execute(select(ProjectMemory).where(
        ProjectMemory.project_id == project.id)
        .order_by(ProjectMemory.key))).scalars().all()
    return [_entry_out(e) for e in rows]


@router.put("/projects/{project_id}/memory")
async def upsert_hub_project_memory(project_id: str, body: MemoryIn,
                                    request: Request, org_id: str | None = None,
                                    db: AsyncSession = Depends(get_db),
                                    user: User = Depends(require_hub_token)):
    from app.api.memory import _entry_out
    from app.models import ProjectMemory
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    project = await _hub_project(db, project_id, org_id)
    existing = (await db.execute(select(ProjectMemory).where(
        ProjectMemory.project_id == project.id,
        ProjectMemory.key == body.key))).scalar_one_or_none()
    from app.core.encryption import encrypt as _encrypt
    if existing:
        existing.value_enc = _encrypt(body.value)
        existing.is_secret = body.is_secret
        existing.description = body.description
        existing.author = "customer"
        entry = existing
    else:
        entry = ProjectMemory(project_id=project.id, author="customer", key=body.key,
                              value_enc=_encrypt(body.value), is_secret=body.is_secret,
                              description=body.description)
        db.add(entry)
    await db.commit()
    return _entry_out(entry)


@router.delete("/projects/{project_id}/memory/{entry_id}")
async def delete_hub_project_memory(project_id: str, entry_id: str,
                                    request: Request, org_id: str | None = None,
                                    db: AsyncSession = Depends(get_db),
                                    user: User = Depends(require_hub_token)):
    from app.models import ProjectMemory
    await rate_limit(request, "hub-project-write", 60, 600, identity=f"hub:{user.id}")
    project = await _hub_project(db, project_id, org_id)
    entry = await db.get(ProjectMemory, entry_id)
    if entry is None or entry.project_id != project.id:
        raise HTTPException(404, "Unknown memory entry")
    await db.delete(entry)
    await db.commit()
    return {"ok": True}
