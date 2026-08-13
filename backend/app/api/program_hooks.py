"""§28 inbound trigger hooks - the public webhook receiver.

`POST /api/programs/hooks/{instance_id}` is unauthenticated, internet-facing and
spends org credits, so every guard is mandatory and ordered cheapest-first:
instance lookup (404 also for disabled - no existence oracle beyond the id
itself), per-instance rate cap, body-size cap, HMAC/token verification against
the per-instance secret, replay dedup on the provider delivery id (redis SET NX,
altcha idiom), event normalization + deterministic filters, the pending-queue
cap, then the credit check. Registered CSRF-free in main.py (billing-webhook
precedent) - signature IS the auth, there is no cookie session.

Always answer 204 for an authenticated delivery we choose not to act on
(non-issue event, filter miss, duplicate, queue full) - a non-2xx would make
the provider retry what we deliberately dropped.
"""
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import rate_limit
from app.core.encryption import decrypt
from app.models import Organization, Program, ProgramInstance, ProgramRun, utcnow
from app.services import program_hooks as hooks_svc
from app.services.events import get_async_redis
from app.workers.celery_app import celery

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/programs/hooks", tags=["program-hooks"])

REPLAY_TTL_S = 86400


@router.post("/{instance_id}", status_code=204)
async def receive(instance_id: str, request: Request,
                  db: AsyncSession = Depends(get_db)) -> Response:
    inst = await db.get(ProgramInstance, instance_id)
    if inst is None or not inst.hook_enabled or not inst.hook_secret_enc:
        raise HTTPException(404, "Not found")
    await rate_limit(request, "program_hook", settings.program_hook_rate_per_minute,
                     60, identity=inst.id)
    body = await request.body()
    if len(body) > hooks_svc.MAX_BODY_BYTES:
        raise HTTPException(413, "Payload too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    provider = hooks_svc.verify_signature(decrypt(inst.hook_secret_enc), headers, body)
    if provider is None:
        raise HTTPException(401, "Invalid signature")

    delivery = hooks_svc.delivery_id(headers, body)
    try:
        fresh = await get_async_redis().set(
            f"proghook:{inst.id}:{delivery}", "1", nx=True, ex=REPLAY_TTL_S)
    except Exception as exc:  # noqa: BLE001 - redis down: accept rather than drop
        log.warning("program hook replay store unavailable: %s", exc)
        fresh = True
    if not fresh:
        return Response(status_code=204)  # provider retry / replay - already handled

    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Body is not JSON")
    event = hooks_svc.normalize_event(provider, headers, payload)
    if event is None or not hooks_svc.event_matches(inst.hook_filters or {}, event):
        return Response(status_code=204)
    event["delivery"] = delivery

    program = await db.get(Program, inst.program_id)
    if program is None or not program.is_published:
        raise HTTPException(404, "Not found")
    pending = (await db.execute(select(func.count(ProgramRun.id)).where(
        ProgramRun.instance_id == inst.id,
        ProgramRun.kind == "hook",
        ProgramRun.state == "queued"))).scalar_one()
    if pending >= settings.program_hook_max_pending:
        log.info("program hook %s: pending cap reached, dropping %s", inst.id, delivery)
        return Response(status_code=204)

    org = await db.get(Organization, inst.org_id)
    if org is None or (org.credit_balance or 0.0) <= 0:
        # Visible failure (the schedule sweep's empty-wallet pattern) instead of
        # a silent drop - the customer sees WHY nothing ran.
        db.add(ProgramRun(id=str(uuid.uuid4()), program_id=program.id,
                          instance_id=inst.id, org_id=inst.org_id, kind="hook",
                          state="failed", error="insufficient credits",
                          started_at=utcnow(), finished_at=utcnow(),
                          hook_event=event))
        await db.commit()
        return Response(status_code=204)

    run = ProgramRun(id=str(uuid.uuid4()), program_id=program.id,
                     instance_id=inst.id, org_id=inst.org_id, kind="hook",
                     hook_event=event)
    db.add(run)
    await db.commit()
    # Eager dispatch: run_program's per-instance serialization guard leaves the
    # run queued when another is running; the sweep's deferred pass drains it.
    celery.send_task("app.workers.programs.run_program", args=[run.id])
    return Response(status_code=204)
