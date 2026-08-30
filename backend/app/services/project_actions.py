"""Shared customer-action service layer (§hub pass-through P0). The SPA routes
and the hub pass-through routes must run the SAME guards and side effects for
every project action, so each action's body lives here keyed on
(db, project, actor) - never on a session User - and the routes stay thin
wrappers (role -> actor derivation, HTTP mapping, serialization). Raises
ActionError(status, detail); callers map it to their transport's error shape.
Each action commits (it is the transaction boundary, exactly as the handlers
it was extracted from did)."""
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import (
    dev_help_capability, dev_resume_capability, dev_run_resume_capability, message_out,
)
from app.core.config import settings
from app.models import CreditTransaction, DevRun, Message, Organization, Project, Request
from app.services import dev_concurrency, events, hub_events
from app.services.lifecycle import TransitionError, transition_async
from app.workers.celery_app import celery


class ActionError(Exception):
    """An action guard failed; `status` maps it to an HTTP response."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def evaluate(db: AsyncSession, project: Project) -> str | None:
    """Run (or synthesize) the §7 evaluation. Returns the Celery task id, or
    None for direct_quote (no AI moderation/estimation - priced by the admin)."""
    if project.status != "draft":
        raise ActionError(409, "Evaluation runs in draft only")
    if project.kind == "direct_quote":
        project.evaluation = {
            "state": "done",
            "moderation": {"allowed": True, "flags": []},
            "feasibility": {"verdict": "pass",
                            "reasons": [f"Direct quote - {settings.consultant_first_name} will review your request "
                                        "and send a custom quote. No charge to submit."]},
            "estimate": {"credits": None, "tokens": None, "cost_per_token": None,
                         "explanation": f"A tailored quote will be provided by {settings.consultant_first_name} "
                                        "after reviewing this project. Submitting is free."},
        }
        await db.commit()
        return None
    project.evaluation = {"state": "pending"}
    await db.commit()
    task = celery.send_task("app.workers.tasks.evaluate_project", args=[project.id])
    return task.id


async def submit(db: AsyncSession, project: Project) -> None:
    """draft -> awaiting_review once the evaluation verdict allows it (§8). A
    review_required verdict blocks auto-development for the admin to decide."""
    ev = project.evaluation or {}
    verdict = (ev.get("feasibility") or {}).get("verdict")
    if ev.get("state") != "done" or verdict not in ("pass", "review_required"):
        raise ActionError(409, "Project must pass evaluation before submission")
    if verdict == "review_required":
        project.block_auto_development = True
    try:
        await transition_async(db, project, "awaiting_review", "customer",
                               "Submitted by customer")
    except TransitionError as exc:
        raise ActionError(409, str(exc))
    await db.commit()


async def require_review(db: AsyncSession, project: Project) -> None:
    """Pull the consultant into an AI project (customer actor; charges
    REVIEW_REQUEST_CREDITS on ai projects, refundable by the admin)."""
    fee = settings.review_request_credits
    charge = project.kind == "ai" and fee > 0
    org = None
    if charge:
        org = await db.get(Organization, project.org_id)
        if (org.credit_balance or 0.0) < fee:
            raise ActionError(402, f"Requesting {settings.consultant_first_name}'s review costs {fee:g} credits; "
                                   f"your balance is too low. Top up to continue.")
    try:
        await transition_async(db, project, "awaiting_admin", "customer",
                               f"Customer requested {settings.consultant_first_name}'s review")
    except TransitionError as exc:
        raise ActionError(409, str(exc))
    if charge:
        await db.execute(update(Organization).where(Organization.id == org.id)
                         .values(credit_balance=func.coalesce(Organization.credit_balance, 0.0) - fee))
        db.expire(org, ["credit_balance"])
        db.add(CreditTransaction(org_id=org.id, project_id=project.id, amount=-fee,
                                 kind="review_request",
                                 detail=f"Requested {settings.consultant_first_name}'s review (refundable)"))
    await db.commit()


async def request_help(db: AsyncSession, project: Project) -> None:
    """§request help: hand a build that failed on a PLATFORM fault to the
    consultant, free.

    `require_review` above is the customer buying attention, so it charges. This
    is the platform conceding that its own machinery broke - the agent driver
    crashed, the model endpoint refused the configured model, the sandbox lost
    the git remote - and charging for that would be charging for our own bug.
    The gate is `dev_help_capability`, the same verdict the button renders, so
    the free path can never be reached over an ordinary build failure.

    Destination is awaiting_admin: the SAME queue the paid review lands in, which
    keeps one consultant inbox instead of two and gets the §8 admin email for
    free. It also takes the project off the customer's Resume, deliberately - a
    platform fault resumed unchanged fails again, and the run's own console still
    carries every detail the fix needs.
    """
    enabled, blocker = dev_help_capability(project)
    if not enabled:
        raise ActionError(409, blocker or "Help isn't available for this build")
    error = (project.dev_run_error or "").strip()
    try:
        await transition_async(db, project, "awaiting_admin", "customer",
                               "Customer asked for help with a build that failed on our side"
                               + (f": {error[:200]}" if error else ""))
    except TransitionError as exc:
        raise ActionError(409, str(exc))
    # Say it in the thread the customer is actually reading - the failed run's
    # request thread, not just the main-thread status line the transition writes.
    thread = f"request:{project.dev_request_id}" if project.dev_request_id else "main"
    if not await valid_thread(db, project, thread):
        thread = "main"
    msg = Message(project_id=project.id, thread=thread, author="agent",
                  body=("That build failed on our side, not yours, so this one is on us: "
                        f"{settings.consultant_first_name} has been alerted and will pick it "
                        "up from here. No credits were charged for asking."))
    db.add(msg)
    await db.flush()
    hub_events.record(db, project, "message", hub_events.message_payload(msg))
    await db.commit()
    await events.publish_async(project.id, {"type": "message", "message": message_out(msg)})


async def charge_chat_upfront(db: AsyncSession, project: Project) -> None:
    """Debit the one-time chat-project opening fee (§chat kind). Runs inside the
    caller's create transaction (no commit here) so a failed create can't strand
    the charge. Raises ActionError(402) when the wallet can't cover it."""
    fee = settings.chat_upfront_credits
    if fee <= 0:
        return
    org = await db.get(Organization, project.org_id)
    if (org.credit_balance or 0.0) < fee:
        raise ActionError(402, f"Opening a chat costs {fee:g} credits; "
                               "your balance is too low. Top up to continue.")
    await db.execute(update(Organization).where(Organization.id == org.id)
                     .values(credit_balance=func.coalesce(Organization.credit_balance, 0.0) - fee))
    db.expire(org, ["credit_balance"])
    db.add(CreditTransaction(org_id=org.id, project_id=project.id, amount=-fee,
                             kind="chat_upfront", detail="Chat opening fee"))


async def run_resume_sets(db: AsyncSession, project: Project) -> tuple[set, set]:
    """The two sets `dev_run_resume_capability` judges a row against: the
    request ids with a run in flight, and the newest failed row per request.
    One gatherer for the serializers (row `can_resume`) and the action, so the
    button and the endpoint can't drift."""
    inflight = set((await db.execute(
        select(DevRun.request_id).where(
            DevRun.project_id == project.id,
            DevRun.state.in_(dev_concurrency.ACTIVE_ROW_STATES)))).scalars().all())
    failed = (await db.execute(
        select(DevRun.id, DevRun.request_id)
        .where(DevRun.project_id == project.id, DevRun.state == "failed")
        .order_by(DevRun.created_at.desc(), DevRun.id.desc()))).all()
    latest: dict = {}
    for run_id, request_id in failed:
        latest.setdefault(request_id, run_id)
    return inflight, set(latest.values())


async def retry_build(db: AsyncSession, project: Project, is_admin: bool,
                      fresh: bool = False, run_id: str | None = None) -> None:
    """Resume development after a failed/stalled build (§14.5). Gated by the same
    dev_resume_capability the UI renders, so button and endpoint can't drift.
    `run_id` (§parallel-builds) resumes ONE failed row - the request-thread
    history console's Resume - under the row's own `dev_run_resume_capability`,
    and chains onto it through its request (`acquire_for_project` continues the
    request's newest failed run, which the capability just proved this is)."""
    await db.refresh(project, ["repos"])
    request_id = None
    if run_id is not None:
        run = await db.get(DevRun, run_id)
        if run is None or run.project_id != project.id:
            raise ActionError(404, "Unknown run")
        inflight, latest = await run_resume_sets(db, project)
        enabled, blocker = dev_run_resume_capability(
            project, run, inflight_request_ids=inflight, latest_failed_ids=latest)
        request_id = run.request_id
    else:
        enabled, blocker = dev_resume_capability(project)
    if not enabled:
        raise ActionError(409, blocker or "Resume isn't available right now")
    if project.status == "awaiting_admin" and not is_admin:
        raise ActionError(409, f"{settings.consultant_first_name} has the project - it resumes after review")
    actor = "admin" if is_admin else "customer"
    if project.status in ("awaiting_customer", "awaiting_admin"):
        try:
            await transition_async(db, project, "development", actor, "Resuming development")
        except TransitionError as exc:
            raise ActionError(409, str(exc))
    # other statuses (e.g. development after a transient failure): the
    # customer-facing status stays put; only the dev sub-state cycles.
    await db.commit()
    # §parallel-builds MR1: take a run slot through the one chokepoint (sync
    # service, threadpool - knowledge.py precedent) before dispatching.
    try:
        new_run_id = await run_in_threadpool(dev_concurrency.acquire_for_project,
                                             project.id, request_id, fresh)
    except dev_concurrency.SlotRefused as exc:
        raise ActionError(409, str(exc))
    celery.send_task("app.workers.tasks.run_development", args=[project.id],
                     kwargs={"fix_only": True, "run_id": new_run_id})


def stop_build(project: Project, run_id: str | None = None) -> None:
    """§14: stop the in-flight build. Only the agent-build phase is stoppable;
    deploying/awaiting_merge belong to other owners. §parallel-builds MR4:
    run_id scopes the stop to ONE sibling (the worker task validates the row
    belongs to this project and is running); without it, the mirror gate and
    the worker's own resolution keep today's single-run behavior."""
    if run_id is None and project.dev_run_state != "running":
        raise ActionError(409, "No build is running")
    celery.send_task("app.workers.tasks.stop_development",
                     args=[project.id, run_id])


# Statuses whose demo may be (re)deployed - the one set both the SPA demo
# routes and the hub pass-through gate on.
DEMO_DEPLOYABLE_STATUSES = {"development", "awaiting_customer", "awaiting_admin", "finished"}


def start_demo(project: Project) -> None:
    """Deploy/redeploy the project's demo container (async via demo_start)."""
    if project.status not in DEMO_DEPLOYABLE_STATUSES:
        raise ActionError(409, "Project is not deployable yet")
    if project.demo_state == "running":
        raise ActionError(409, "Demo already running")
    celery.send_task("app.workers.tasks.demo_start", args=[project.id, "start"])


def stop_demo(project: Project) -> None:
    """Stop the running demo container (async via demo_stop)."""
    if project.demo_state != "running":
        raise ActionError(409, "Demo is not running")
    celery.send_task("app.workers.tasks.demo_stop", args=[project.id, "stop"])


async def approve_delivery(db: AsyncSession, project: Project, actor: str) -> None:
    """The customer accepts the delivered MVP once the demo is live, moving the
    project to `finished` (§ delivery acceptance)."""
    if project.kind == "direct_quote":
        raise ActionError(409, "Direct-quote engagements are closed by the admin")
    if not project.demo_deployed_once:
        raise ActionError(409, "Approve delivery once your demo has been deployed")
    try:
        await transition_async(db, project, "finished", actor,
                               "Delivery approved by the customer")
    except TransitionError as exc:
        raise ActionError(409, str(exc))
    # §threads Request #0 is closed by the transition itself (services/lifecycle):
    # every route to a terminal status closes it, not just this one.
    await db.commit()


MVP_REQUEST_TITLE = "Initial build"


async def mvp_request(db: AsyncSession, project: Project) -> Request | None:
    """§threads Request #0: the request row anchoring the initial MVP build's
    thread. None for projects born before MVP requests existed (their build
    narration stays in main)."""
    return (await db.execute(
        select(Request).filter_by(project_id=project.id, type="mvp")
        .order_by(Request.created_at))).scalars().first()


async def create_mvp_request(db: AsyncSession, project: Project) -> Request:
    """§threads Request #0: every ai-kind project is born with its initial build
    as a Request, so the whole build conversation (narration, failures, steering
    replies) lives in that request's thread and the main thread stays the
    orchestrator. Server-side only - the client request schemas' type Literal
    can never mint an 'mvp' row. Called inside the project-create transaction;
    the caller commits."""
    req = Request(project_id=project.id, type="mvp", handling="ai",
                  status="open", title=MVP_REQUEST_TITLE)
    db.add(req)
    await db.flush()
    # Seed the thread with the customer's ask, as create_request does: it gives
    # the thread standalone context and MR1's steering transcript a base.
    msg = Message(project_id=project.id, thread=f"request:{req.id}",
                  author="customer", body=project.description)
    db.add(msg)
    await db.flush()
    hub_events.record(db, project, "message", hub_events.message_payload(msg))
    return req


async def valid_thread(db: AsyncSession, project: Project, thread: str) -> bool:
    if thread == "main":
        return True
    if thread.startswith("request:"):
        req = await db.get(Request, thread.split(":", 1)[1])
        return req is not None and req.project_id == project.id
    return False


async def post_chat_message(db: AsyncSession, project: Project, author: str,
                            thread: str, body: str,
                            also_email: bool = False,
                            image_ids: list[str] | None = None) -> Message:
    """Append an immutable chat message as `author` ('customer' | 'admin') and run
    the message side effects: WS publish, optional admin email, and the §12
    chat-intent classifier on human main-thread messages.

    §chat images: `image_ids` claims images this author already uploaded to this
    project (see api/chat_images) - they are recorded on Message.meta so every
    reader, including the hub, sees them without a second query."""
    from app.api.chat_images import link_to_message

    if not await valid_thread(db, project, thread):
        raise ActionError(404, "Unknown thread")
    msg = Message(project_id=project.id, thread=thread, author=author, body=body,
                  emailed=bool(also_email and author == "admin"))
    db.add(msg)
    await db.flush()
    if image_ids:
        images = await link_to_message(db, project, msg.id, image_ids, author)
        if images:
            msg.meta = {**(msg.meta or {}), "images": images}
    hub_events.record(db, project, "message", hub_events.message_payload(msg))
    await db.commit()
    await events.publish_async(project.id, {"type": "message", "message": message_out(msg)})
    if author == "admin" and also_email:
        celery.send_task("app.workers.tasks.email_chat_message", args=[project.id, msg.id])
    if project.kind == "chat":
        # §chat kind: customer main-thread messages get a KB-grounded answer, and
        # the §12 classifier never runs (there is no dev pipeline to drive). Admin
        # messages are the human consultant joining - the agent stays out of the way.
        if settings.chat_answer_enabled and thread == "main" and author == "customer":
            celery.send_task("app.workers.tasks.answer_chat_message",
                             args=[project.id, msg.id])
    elif settings.chat_classify_enabled and (
            thread == "main" or thread.startswith("request:")):
        # §threads live threads: request-thread replies classify too, with a
        # reduced action set scoped to that request (confirm/resume - see
        # _classify_thread_message).
        celery.send_task("app.workers.tasks.classify_chat_message",
                         args=[project.id, msg.id])
    return msg


async def create_request(db: AsyncSession, project: Project, author: str,
                         rtype: str, handling: str, body: str,
                         repo_id: str | None = None) -> tuple[Request, Message]:
    """File a §12 Request with its opening thread message and dispatch the
    follow-ups (LLM title pass; AI-handled requests spawn the scoped dev job,
    manual/production_deploy ones notify the admin). §repo binding: `repo_id`
    pins the request's builds to one connected repo (null = the project's
    default push target at dispatch time); a foreign or unknown id 404s."""
    from app.models import ProjectRepo
    from app.services import naming

    if project.kind == "chat":
        raise ActionError(409, "Chat projects don't take development requests - just ask in the chat")
    if repo_id:
        row = await db.get(ProjectRepo, repo_id)
        if row is None or row.project_id != project.id:
            raise ActionError(404, "Unknown repository for this project")
    handling = "manual" if rtype == "production_deploy" else handling
    req = Request(project_id=project.id, type=rtype, handling=handling,
                  repo_id=repo_id or None,
                  title=naming.name_from_description(body))
    db.add(req)
    await db.flush()
    msg = Message(project_id=project.id, thread=f"request:{req.id}", author=author,
                  body=body)
    db.add(msg)
    await db.flush()
    hub_events.record(db, project, "message", hub_events.message_payload(msg))
    await db.commit()
    await events.publish_async(project.id, {"type": "message", "message": message_out(msg)})
    celery.send_task("app.workers.tasks.title_request", args=[req.id, req.title])
    if rtype == "production_deploy":
        celery.send_task("app.workers.tasks.notify_admin_request", args=[project.id, req.id])
    elif handling == "ai":
        celery.send_task("app.workers.tasks.handle_request",
                         args=[project.id, req.id, msg.id])
    elif author == "customer":
        celery.send_task("app.workers.tasks.notify_admin_request", args=[project.id, req.id])
    return req, msg


async def start_request(db: AsyncSession, project: Project, request_id: str,
                        actor: str = "customer") -> Request:
    """Start an AI-handled request (§12/§14): the Requests-tab counterpart to a
    "go ahead" chat reply. handle_request itself gates on credits/buildable/
    no-run-in-flight. Starting a PROPOSED request is the go-ahead moment
    (the chat ack's ✓ button and the Requests tab both land here), so it posts
    the same main-thread trail the classifier's confirm verdict would - the
    actor's canned "Go ahead" reply, then the agent's dispatch ack - keeping the
    conversation a faithful log and freezing the ack panel (shared-ui
    confirmState matches the canned reply verbatim)."""
    req = await db.get(Request, request_id)
    if req is None or req.project_id != project.id:
        raise ActionError(404, "Unknown request")
    if req.type == "mvp":
        raise ActionError(409, "The initial build is driven from the project "
                               "overview - use Resume development there")
    if req.handling != "ai" or req.type == "production_deploy":
        raise ActionError(409, "This request isn't AI-handled")
    if req.status not in ("proposed", "open"):
        raise ActionError(409, "This request has already been started")
    was_proposed = req.status == "proposed"
    req.status = "open"
    published: list[Message] = []
    if was_proposed:
        for author, body in (
            ("admin" if actor == "admin" else "customer", "Go ahead"),
            ("agent", f'On it - starting "{req.title}". Follow progress here: '
                      f"{settings.app_base_url}/projects/{project.id}/requests/{req.id}"),
        ):
            m = Message(project_id=project.id, thread="main", author=author, body=body)
            db.add(m)
            await db.flush()
            hub_events.record(db, project, "message", hub_events.message_payload(m))
            published.append(m)
    await db.commit()
    for m in published:
        await events.publish_async(project.id, {"type": "message", "message": message_out(m)})
    celery.send_task("app.workers.tasks.handle_request", args=[project.id, req.id, ""])
    return req


async def cancel_request(db: AsyncSession, project: Project, actor: str,
                         request_id: str) -> Request:
    """§requests: the customer (or admin) cancels an AI-handled request - the
    negative twin of validate_request. The request closes rejected; a run of
    its parked awaiting_merge closes failed ("canceled") and the work unit
    ends the way dev_pr_sweep ends rejected work: branch + PR pointers cleared
    so a future build starts fresh, the mirror repointed at the newest
    still-active sibling, else settled idle (the console retracts - nothing is
    resumable or watched anymore; an open PR left on the repo is the
    customer's to close). A run still queued/running/deploying is NEVER
    touched - stop it first, then cancel."""
    from app.models import DevRun

    req = await db.get(Request, request_id)
    if req is None or req.project_id != project.id:
        raise ActionError(404, "Unknown request")
    if req.type == "mvp":
        raise ActionError(409, "The initial build can't be canceled from here - "
                               "cancel the project instead")
    if req.handling != "ai" or req.type == "production_deploy":
        raise ActionError(409, "Only AI-handled requests can be canceled - "
                               f"ask {settings.consultant_first_name} to close manual work")
    if req.status in ("done", "rejected"):
        raise ActionError(409, "This request is already closed")
    active = (await db.execute(select(DevRun).where(
        DevRun.request_id == req.id,
        DevRun.state.in_(dev_concurrency.ACTIVE_ROW_STATES)))).scalars().all()
    if any(r.state in ("queued", "running", "deploying") for r in active):
        raise ActionError(409, "A build for this request is still in flight - "
                               "stop it first, then cancel")
    for r in active:  # awaiting_merge only, per the guard above
        r.state = "failed"
        r.run_error = "Request canceled" + (" by the customer" if actor != "admin" else "")
    was_proposed = req.status == "proposed"
    req.status = "rejected"
    declined: list[Message] = []
    if was_proposed:
        # §12 one-click dismiss: declining a PROPOSAL mirrors start_request's
        # confirm trail in the main thread (the canned "Not now" freezes the ack
        # panel); canceling already-started work stays out of the main thread.
        for author, body in (
            ("admin" if actor == "admin" else "customer", "Not now"),
            ("agent", f'Okay - I\'ve dropped "{req.title}". Ask again here '
                      "whenever you want it."),
        ):
            m = Message(project_id=project.id, thread="main", author=author, body=body)
            db.add(m)
            await db.flush()
            hub_events.record(db, project, "message", hub_events.message_payload(m))
            declined.append(m)
    if project.dev_request_id == req.id or active:
        newest = (await db.execute(select(DevRun).where(
            DevRun.project_id == project.id,
            DevRun.state.in_(dev_concurrency.ACTIVE_ROW_STATES),
            DevRun.state != "queued")
            .order_by(DevRun.started_at.desc().nulls_last())
            .limit(1))).scalar_one_or_none()
        if newest is not None:
            project.dev_run_state = newest.state
            project.dev_run_started_at = newest.started_at
            project.dev_request_id = newest.request_id
            project.dev_branch = newest.branch
            project.dev_pr_number = newest.pr_number
            project.dev_pr_url = newest.pr_url
        elif project.dev_request_id == req.id:
            # End the work unit exactly as the sweep ends rejected work - and
            # retract the console: nothing of this request is watched anymore.
            project.dev_run_state = "idle"
            project.dev_request_id = None
            project.dev_branch = None
            project.dev_pr_number = None
            project.dev_pr_url = None
            project.dev_run_error = None
    msg = Message(project_id=project.id, thread=f"request:{req.id}", author="system",
                  body="Request canceled"
                       + (f" by {settings.consultant_first_name}." if actor == "admin"
                          else " by the customer."))
    db.add(msg)
    await db.flush()
    hub_events.record(db, project, "message", hub_events.message_payload(msg))
    await db.commit()
    for m in declined:
        await events.publish_async(project.id, {"type": "message", "message": message_out(m)})
    await events.publish_async(project.id, {"type": "message", "message": message_out(msg)})
    await events.publish_async(project.id, {"type": "dev",
                                            "dev_run_state": project.dev_run_state,
                                            "project_id": project.id})
    return req


async def validate_request(db: AsyncSession, project: Project, actor: str,
                           request_id: str) -> Request:
    """§requests: the customer (or admin) marks an AI-handled request DELIVERED
    by hand - the per-request twin of approve_delivery under the §8 philosophy
    (the agent can never approve; the human always can). The escape hatch for
    work that landed outside the pipeline's happy path - e.g. a run that pushed
    its commits onto an already-open pull request and then exited non-zero, so
    the platform parked it failed while the deliverable sits merged-able on the
    repo. The request closes done; a run of its parked in awaiting_merge closes
    with it (state done) so the merge sweep stops watching a change the human
    has declared settled; the Project.dev_* display mirror repoints at the
    newest still-active sibling, else settles on done. A run still queued/
    running/deploying is NEVER touched - stop it first, then validate."""
    from app.models import DevRun

    req = await db.get(Request, request_id)
    if req is None or req.project_id != project.id:
        raise ActionError(404, "Unknown request")
    if req.type == "mvp":
        raise ActionError(409, "The initial build is approved from the project "
                               "overview - use Approve delivery there")
    if req.handling != "ai" or req.type == "production_deploy":
        raise ActionError(409, "Only AI-handled requests can be validated - "
                               f"{settings.consultant_first_name} closes manual work")
    if req.status in ("done", "rejected"):
        raise ActionError(409, "This request is already closed")
    active = (await db.execute(select(DevRun).where(
        DevRun.request_id == req.id,
        DevRun.state.in_(dev_concurrency.ACTIVE_ROW_STATES)))).scalars().all()
    if any(r.state in ("queued", "running", "deploying") for r in active):
        raise ActionError(409, "A build for this request is still in flight - "
                               "stop it first, then validate")
    for r in active:  # awaiting_merge only, per the guard above
        r.state = "done"
    req.status = "done"
    # Keep the display mirror honest (§parallel-builds): repoint at the newest
    # still-active sibling, else settle the closed run's console on done.
    if project.dev_request_id == req.id or active:
        newest = (await db.execute(select(DevRun).where(
            DevRun.project_id == project.id,
            DevRun.state.in_(dev_concurrency.ACTIVE_ROW_STATES),
            DevRun.state != "queued")
            .order_by(DevRun.started_at.desc().nulls_last())
            .limit(1))).scalar_one_or_none()
        if newest is not None:
            project.dev_run_state = newest.state
            project.dev_run_started_at = newest.started_at
            project.dev_request_id = newest.request_id
            project.dev_branch = newest.branch
            project.dev_pr_number = newest.pr_number
            project.dev_pr_url = newest.pr_url
        elif project.dev_request_id == req.id:
            project.dev_run_state = "done"
    # The paper trail lives in the request's own thread, in the actor's voice.
    msg = Message(project_id=project.id, thread=f"request:{req.id}", author="system",
                  body="Request validated as delivered"
                       + (f" by {settings.consultant_first_name}." if actor == "admin"
                          else " by the customer."))
    db.add(msg)
    await db.flush()
    hub_events.record(db, project, "message", hub_events.message_payload(msg))
    await db.commit()
    await events.publish_async(project.id, {"type": "message", "message": message_out(msg)})
    await events.publish_async(project.id, {"type": "dev",
                                            "dev_run_state": project.dev_run_state,
                                            "project_id": project.id})
    return req
