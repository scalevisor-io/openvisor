"""Apply a status transition with all §8 side effects (status_change row,
system chat message, WS event, notification emails). Async (API) and sync
(Celery) variants share the same rules from services.statuses."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Message, Project, Request, StatusChange, User
from app.services import brand, events, hub_events
from app.services.statuses import can_transition, emails_for


class TransitionError(Exception):
    pass


def _subject(project: Project, to_status: str) -> str:
    return brand.subject(f"{project.name}: status → {to_status}")


def _body(project: Project, to_status: str, reason: str | None) -> str:
    url = f"{settings.app_base_url}/projects/{project.id}"
    lines = [f"Project '{project.name}' is now: {to_status}."]
    if reason:
        lines.append(f"Note: {reason}")
    lines.append(url)
    return "\n".join(lines)


def _plan_emails(project: Project, customer_email: str | None, from_s, to_s, reason):
    plan = emails_for(from_s, to_s)
    out = []
    if plan.to_admin:
        out.append((settings.admin_email, _subject(project, to_s), _body(project, to_s, reason)))
    if plan.to_customer and customer_email:
        out.append((customer_email, _subject(project, to_s), _body(project, to_s, reason)))
    return out


# §threads Request #0: a terminal project closes its initial-build request. The
# two endings mean different things and the request must not lie about which:
# `finished` is a delivery the customer accepted, so the MVP really is done;
# `canceled` never delivered anything, so it is rejected. Living here rather
# than in approve_delivery means EVERY route to a terminal status closes it -
# the admin status route and the hub included, which is what left projects
# closed while their Request #0 sat "in_progress" forever (and, for a project
# with no MVP phase, forever meant forever: nothing else could ever close it).
_TERMINAL_MVP_STATUS = {"finished": "done", "canceled": "rejected"}


def _mvp_close_status(to_status: str) -> str | None:
    return _TERMINAL_MVP_STATUS.get(to_status)


def _open_mvp_query(project: Project):
    return select(Request).where(
        Request.project_id == project.id, Request.type == "mvp",
        Request.status.notin_(("done", "rejected")))


async def transition_async(
    db: AsyncSession, project: Project, to_status: str, actor: str, reason: str | None = None
) -> Project:
    from app.workers.celery_app import celery  # late import (avoid cycle)

    if project.status == to_status:
        return project
    if not can_transition(project.status, to_status, actor):
        raise TransitionError(f"{project.status} → {to_status} not allowed for {actor}")
    from_s = project.status
    project.status = to_status
    db.add(StatusChange(project_id=project.id, from_status=from_s, to_status=to_status,
                        actor=actor, reason=reason))
    msg = Message(project_id=project.id, thread="main", author="system",
                  body=f"Status → {to_status}" + (f" - {reason}" if reason else ""))
    db.add(msg)
    await db.flush()
    hub_events.record(db, project, "status",
                      {"from": from_s, "to": to_status, "actor": actor, "reason": reason})
    hub_events.record(db, project, "message", hub_events.message_payload(msg))

    mvp_status = _mvp_close_status(to_status)
    if mvp_status:
        mvp = (await db.execute(_open_mvp_query(project))).scalars().first()
        if mvp is not None:
            mvp.status = mvp_status

    owner = (await db.execute(
        select(User).where(User.org_id == project.org_id).order_by(User.created_at)
    )).scalars().first()
    for to, subject, body in _plan_emails(project, owner.email if owner else None, from_s, to_status, reason):
        celery.send_task("app.workers.tasks.send_email", args=[to, subject, body])

    await events.publish_async(project.id, {"type": "status", "project_id": project.id, "status": to_status})
    await events.publish_async(project.id, {"type": "message", "message": {
        "id": msg.id, "thread": "main", "author": "system", "body": msg.body,
        "emailed": False, "created_at": msg.created_at}})
    return project


def transition_sync(
    db: Session, project: Project, to_status: str, actor: str, reason: str | None = None
) -> Project:
    from app.workers.celery_app import celery

    if project.status == to_status:
        return project
    if not can_transition(project.status, to_status, actor):
        raise TransitionError(f"{project.status} → {to_status} not allowed for {actor}")
    from_s = project.status
    project.status = to_status
    db.add(StatusChange(project_id=project.id, from_status=from_s, to_status=to_status,
                        actor=actor, reason=reason))
    msg = Message(project_id=project.id, thread="main", author="system",
                  body=f"Status → {to_status}" + (f" - {reason}" if reason else ""))
    db.add(msg)
    db.flush()
    hub_events.record(db, project, "status",
                      {"from": from_s, "to": to_status, "actor": actor, "reason": reason})
    hub_events.record(db, project, "message", hub_events.message_payload(msg))

    mvp_status = _mvp_close_status(to_status)
    if mvp_status:
        mvp = db.execute(_open_mvp_query(project)).scalars().first()
        if mvp is not None:
            mvp.status = mvp_status

    owner = db.execute(
        select(User).where(User.org_id == project.org_id).order_by(User.created_at)
    ).scalars().first()
    for to, subject, body in _plan_emails(project, owner.email if owner else None, from_s, to_status, reason):
        celery.send_task("app.workers.tasks.send_email", args=[to, subject, body])

    events.publish_sync(project.id, {"type": "status", "project_id": project.id, "status": to_status})
    events.publish_sync(project.id, {"type": "message", "message": {
        "id": msg.id, "thread": "main", "author": "system", "body": msg.body,
        "emailed": False, "created_at": msg.created_at}})
    return project
