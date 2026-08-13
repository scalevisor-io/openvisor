"""Immutable chat: messages + WebSocket live updates + requests threads."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import message_out, request_out
from app.core.config import settings
from app.core.db import async_session, get_db
from app.core import deps
from app.core.deps import get_current_user, get_project_for_user
from app.core.security import SESSION_COOKIE, read_session_token
from app.models import Message, Project, Request, User
from app.schemas.schemas import HumanAnswerIn, MessageIn, RequestIn, RequestUpdateIn
from app.services import brand, events, hub_events, project_actions
from app.services.lifecycle import TransitionError, transition_async
from app.workers.celery_app import celery

router = APIRouter(prefix="/api/projects/{project_id}", tags=["chat"])

_valid_thread = project_actions.valid_thread


@router.get("/messages")
async def list_messages(thread: str = "main",
                        project: Project = Depends(get_project_for_user),
                        db: AsyncSession = Depends(get_db)):
    if not await _valid_thread(db, project, thread):
        raise HTTPException(404, "Unknown thread")
    rows = (await db.execute(select(Message).where(
        Message.project_id == project.id, Message.thread == thread)
        .order_by(Message.created_at))).scalars().all()
    return [message_out(m) for m in rows]


@router.post("/messages", status_code=201)
async def post_message(body: MessageIn,
                       project: Project = Depends(get_project_for_user),
                       user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    author = "admin" if user.role == "admin" else "customer"
    try:
        msg = await project_actions.post_chat_message(
            db, project, author, body.thread, body.body, also_email=body.also_email,
            image_ids=body.image_ids)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return message_out(msg)


@router.post("/request-human-answer")
async def request_human_answer(body: HumanAnswerIn,
                               project: Project = Depends(get_project_for_user),
                               user: User = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)):
    """§12: the customer explicitly asks the consultant to pick a thread up (replaces
    the removed classifier auto-defer). Free of charge; moves the project to
    awaiting_admin, which emails the admin."""
    if not await _valid_thread(db, project, body.thread):
        raise HTTPException(404, "Unknown thread")
    actor = "admin" if user.role == "admin" else "customer"
    msg = Message(project_id=project.id, thread=body.thread, author="agent",
                  body=f"{settings.consultant_first_name} has been notified and will answer here.")
    db.add(msg)
    await db.flush()
    hub_events.record(db, project, "message", hub_events.message_payload(msg))
    # transition_async no-ops (and skips the admin email) when the project is
    # already awaiting_admin - keep the §12 "admin always notified" guarantee.
    already_admin = project.status == "awaiting_admin"
    try:
        await transition_async(db, project, "awaiting_admin", actor,
                               "Customer requested a human answer")
    except TransitionError as exc:
        raise HTTPException(409, str(exc))
    await db.commit()
    if already_admin:
        celery.send_task("app.workers.tasks.send_email", args=[
            settings.admin_email, brand.subject(f"{project.name}: human answer requested"),
            f"A customer asked for a human answer in chat.\n"
            f"{settings.app_base_url}/projects/{project.id}"])
    await events.publish_async(project.id, {"type": "message", "message": message_out(msg)})
    return {"ok": True}


@router.get("/requests")
async def list_requests(project: Project = Depends(get_project_for_user),
                        db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Request).where(Request.project_id == project.id)
                             .order_by(Request.created_at.desc()))).scalars().all()
    return [request_out(r) for r in rows]


@router.post("/requests/estimate", status_code=202)
async def estimate_request(body: RequestIn,
                           project: Project = Depends(get_project_for_user)):
    """Async cost/time estimate for a request being drafted (informational,
    never a quote). Poll GET /requests/estimate/{task_id} for the result."""
    task = celery.send_task("app.workers.tasks.estimate_request", args=[
        project.id, {"type": body.type, "body": body.body}])
    return {"task_id": task.id}


@router.get("/requests/estimate/{task_id}")
async def estimate_result(task_id: str,
                          project: Project = Depends(get_project_for_user)):
    res = celery.AsyncResult(task_id)
    if not res.ready():
        return {"state": "pending"}
    if res.failed():
        return {"state": "failed"}
    data = dict(res.result or {})
    # The task stamps its project; don't serve another project's estimate.
    if data.pop("project_id", None) != project.id:
        raise HTTPException(404, "Unknown estimate")
    return {"state": "done", "estimate": data}


@router.post("/requests", status_code=201)
async def create_request(body: RequestIn,
                         project: Project = Depends(get_project_for_user),
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    author = "admin" if user.role == "admin" else "customer"
    try:
        req, _ = await project_actions.create_request(
            db, project, author, body.type, body.handling, body.body,
            repo_id=body.repo_id)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return request_out(req)


@router.patch("/requests/{request_id}")
async def update_request(request_id: str, body: RequestUpdateIn,
                         project: Project = Depends(get_project_for_user),
                         db: AsyncSession = Depends(get_db)):
    """Rename a request (the title is machine-generated at creation). Editing
    also stops the async title pass from applying, if it hasn't yet."""
    req = await db.get(Request, request_id)
    if req is None or req.project_id != project.id:
        raise HTTPException(404, "Unknown request")
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "The request title can't be empty")
    req.title = title
    await db.commit()
    return request_out(req)


@router.post("/requests/{request_id}/start")
async def start_request(request_id: str,
                        project: Project = Depends(get_project_for_user),
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """Start an AI-handled request the agent proposed from chat (§12/§14).
    Customer and admin can both start it."""
    try:
        req = await project_actions.start_request(
            db, project, request_id, actor="admin" if user.role == "admin" else "customer")
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return request_out(req)


@router.post("/requests/{request_id}/cancel")
async def cancel_request(request_id: str,
                         project: Project = Depends(get_project_for_user),
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    """§requests: cancel an AI-handled request - the negative twin of validate.
    Closes the request (and parks its awaiting-merge run as canceled); never
    touches a live build."""
    try:
        req = await project_actions.cancel_request(
            db, project, "admin" if user.role == "admin" else "customer", request_id)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return request_out(req)


@router.post("/requests/{request_id}/validate")
async def validate_request(request_id: str,
                           project: Project = Depends(get_project_for_user),
                           user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    """§requests: mark an AI-handled request delivered by hand - the escape
    hatch for work that landed while the pipeline reported failure. Closes the
    request (and its parked awaiting-merge run); never touches a live build."""
    try:
        req = await project_actions.validate_request(
            db, project, "admin" if user.role == "admin" else "customer", request_id)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return request_out(req)


ws_router = APIRouter()


@ws_router.websocket("/ws/projects/{project_id}")
async def project_ws(websocket: WebSocket, project_id: str):
    token = websocket.cookies.get(SESSION_COOKIE)
    uid = read_session_token(token) if token else None
    if uid is None:
        await websocket.close(code=4401)
        return
    async with async_session() as db:
        user = await db.get(User, uid)
        project = await db.get(Project, project_id)
        if (user is None or project is None
                or await deps.project_access_role(db, user, project) is None):
            await websocket.close(code=4404)
            return
    await websocket.accept()
    r = events.get_async_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(events.channel(project_id))

    async def forward():
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=25)
            if msg is not None:
                await websocket.send_text(msg["data"])
            else:
                await websocket.send_text(json.dumps({"type": "ping"}))

    forwarder = asyncio.create_task(forward())
    try:
        while True:  # drain client frames; raises on disconnect
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        forwarder.cancel()
        await pubsub.unsubscribe(events.channel(project_id))
        await pubsub.aclose()
