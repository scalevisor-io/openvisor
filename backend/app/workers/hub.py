"""Spoke -> hub Celery tasks (PROMPT hub link). All are instant no-ops when no
hub is configured (settings.hub_mcp_url empty), so a standalone spoke is
unaffected. Wired into Beat: hub_heartbeat every 60 s, hub_usage_report every
300 s, hub_project_events_report every 60 s. None may crash Beat - every
network/hub error is caught and logged."""
import logging
from datetime import datetime

from sqlalchemy import func, or_, select

from app.core.config import settings
from app.core.db import SyncSession
from app.models import CreditTransaction, HubCreditGrant, HubProjectEvent, Organization, utcnow
from app.services import app_settings, hub_client
from app.version import get_version
from app.workers.celery_app import celery

log = logging.getLogger(__name__)

REGISTERED_KEY = "hub_registered"
USAGE_CURSOR_KEY = "hub_usage_cursor"
USAGE_BATCH = 500  # at-least-once: only advance the cursor after the hub acks
PROJECT_EVENT_BATCH = 200


def _wallet_snapshot(db) -> dict:
    total = db.execute(
        select(func.coalesce(func.sum(Organization.credit_balance), 0.0))).scalar_one()
    orgs = db.execute(select(func.count()).select_from(Organization)).scalar_one()
    return {"org_count": orgs, "credit_balance_total": round(total or 0.0, 4)}


@celery.task(name="app.workers.hub.hub_heartbeat")
def hub_heartbeat() -> None:
    if not settings.hub_mcp_url:
        return
    try:
        with SyncSession() as db:
            payload = {"deploy_domain": settings.deploy_domain, "version": get_version(),
                       "wallet": _wallet_snapshot(db)}
            if not bool(app_settings.get_setting_sync(db, REGISTERED_KEY, False)):
                hub_client.register_spoke(payload)
                app_settings.set_setting_sync(db, REGISTERED_KEY, True)
                db.commit()
            hub_client.heartbeat(payload)
    except hub_client.HubError as exc:
        log.warning("hub heartbeat failed: %s", exc)
    except Exception:  # never crash Beat
        log.exception("hub heartbeat unexpected error")


@celery.task(name="app.workers.hub.hub_usage_report")
def hub_usage_report() -> None:
    if not settings.hub_mcp_url:
        return
    try:
        with SyncSession() as db:
            cursor = app_settings.get_setting_sync(db, USAGE_CURSOR_KEY, None)
            # Same privacy boundary as the pull endpoints (api/hub.py): only
            # hub-relevant orgs (hub-created or hub-funded) are reported. The
            # hub never needed direct-customer events - its accrual only fees
            # funded orgs - so they must not leave the spoke.
            hub_orgs = select(Organization.id).where(or_(
                Organization.hub_managed.is_(True),
                Organization.id.in_(select(HubCreditGrant.org_id)),
            )).scalar_subquery()
            q = (select(CreditTransaction)
                 .where(CreditTransaction.org_id.in_(hub_orgs))
                 .order_by(CreditTransaction.created_at))
            if cursor:
                q = q.where(CreditTransaction.created_at > datetime.fromisoformat(cursor))
            rows = db.execute(q.limit(USAGE_BATCH)).scalars().all()
            if not rows:
                return
            # Hub contract: ext_id (this spoke's transaction id) + occurred_at are
            # the dedup/cursor keys hub-side; extra fields ride along as context.
            events = [{"ext_id": t.id, "org_id": t.org_id, "project_id": t.project_id,
                       "amount": round(t.amount, 4), "kind": t.kind, "detail": t.detail,
                       "occurred_at": t.created_at.isoformat()} for t in rows]
            result = hub_client.report_usage(events)  # raises HubError on transport/tool error
            acked = int(result.get("acked", 0)) if isinstance(result, dict) else 0
            if acked != len(events):
                # A partial ack means the hub dropped events - do NOT advance the
                # cursor past them (at-least-once; the hub dedups replays on ext_id).
                raise hub_client.HubError(
                    f"hub acked {acked}/{len(events)} usage events; cursor not advanced")
            app_settings.set_setting_sync(db, USAGE_CURSOR_KEY, rows[-1].created_at.isoformat())
            db.commit()
    except hub_client.HubError as exc:
        log.warning("hub usage report failed: %s", exc)
    except Exception:  # never crash Beat
        log.exception("hub usage report unexpected error")


@celery.task(name="app.workers.hub.hub_project_events_report")
def hub_project_events_report() -> None:
    """Push unsent from-hub project outbox rows to the hub (§pass-through P1).
    Claim-based, not cursor-based: rows are selected with sent_at IS NULL under
    FOR UPDATE SKIP LOCKED and stamped sent_at only after the hub acks the FULL
    batch - so equal timestamps and out-of-order commits can never skip an event
    (the hazard a timestamp cursor has). At-least-once; the hub dedups on id."""
    if not settings.hub_mcp_url:
        return
    try:
        with SyncSession() as db:
            rows = db.execute(
                select(HubProjectEvent)
                .where(HubProjectEvent.sent_at.is_(None))
                .order_by(HubProjectEvent.created_at)
                .limit(PROJECT_EVENT_BATCH)
                .with_for_update(skip_locked=True)).scalars().all()
            if not rows:
                return
            events = [{"id": e.id, "project_id": e.project_id, "hub_ref": e.hub_ref,
                       "etype": e.etype, "payload": e.payload,
                       "occurred_at": e.created_at.isoformat()} for e in rows]
            result = hub_client.report_project_events(events)
            acked = int(result.get("acked", 0)) if isinstance(result, dict) else 0
            if acked != len(events):
                raise hub_client.HubError(
                    f"hub acked {acked}/{len(events)} project events; batch not marked sent")
            now = utcnow()
            for e in rows:
                e.sent_at = now
            db.commit()
    except hub_client.HubError as exc:
        log.warning("hub project-events report failed: %s", exc)
    except Exception:  # never crash Beat
        log.exception("hub project-events report unexpected error")
