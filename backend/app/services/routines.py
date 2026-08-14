"""§routines: saved prompts on a project, optionally scheduled.

A routine is a template. Firing one creates an ordinary `Request` seeded with
the saved prompt and hands it to the normal dispatch path - so threads, the dev
run, the PR/MR flow and billing are all the existing machinery, not a parallel
pipeline. `fire()` is SYNC on purpose: the Celery sweep and the API's "Run now"
must take the exact same path, and the worker side is sync, so the async route
calls `fire_now()` through a threadpool rather than growing a second
implementation that can drift.

Why the guards are what they are: the auto_dev sweep gets idempotence for free
(it dedups on the issue URL, so re-seeing an issue never refiles it), but a
routine has no such key - running the same prompt every Monday IS the feature.
`_blocked_reason` is therefore the whole safety story, and every refusal is
recorded on the row so a routine that quietly does nothing can always explain
itself.
"""
import logging
from datetime import datetime

from croniter import croniter
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    Organization, Project, ProjectRoutine, Request, utcnow,
)
from app.services import app_settings, dev_concurrency

log = logging.getLogger(__name__)

# Instance-level kill switch (§routines). Stored as "disabled" so a missing row
# reads as enabled, matching the pause_* flags: the feature works out of the box
# and an admin turns it OFF, which is also the shape a future paid tier needs.
ROUTINES_DISABLED = "routines_disabled"

OPEN_REQUEST_STATES = ("open", "quoted", "in_progress")
TITLE_MAX = 255
PROMPT_MAX = 8000


class RoutineError(ValueError):
    """A refusal with customer-facing copy (the API maps it to 409/400)."""


def enabled_sync(db: Session) -> bool:
    """Is the feature on for this instance? Read on every sweep tick and every
    customer write, so switching it off takes effect immediately without a
    deploy."""
    return not bool(app_settings.get_setting_sync(db, ROUTINES_DISABLED, False))


def validate_cron(expr: str) -> str | None:
    """None when the expression is valid AND respects the routine floor; else a
    human-readable error. A routine fires REAL dev runs, so its floor is its own
    setting (hours, not the program sweep's minutes) - the cost of a mistyped
    `* * * * *` here is a build every minute."""
    expr = (expr or "").strip()
    if not expr:
        return "cron expression is empty"
    try:
        it = croniter(expr, utcnow())
    except (ValueError, KeyError) as exc:
        return f"invalid cron expression: {exc}"
    floor_s = settings.routine_min_schedule_minutes * 60 - 30  # slack for uneven crons
    prev = it.get_next(datetime)
    for _ in range(8):
        nxt = it.get_next(datetime)
        if (nxt - prev).total_seconds() < floor_s:
            return (f"routine runs more often than every "
                    f"{settings.routine_min_schedule_minutes} minutes (platform floor)")
        prev = nxt
    return None


def next_run(expr: str, after=None):
    return croniter(expr, after or utcnow()).get_next(datetime)


def _blocked_reason(db: Session, routine: ProjectRoutine, project: Project) -> str | None:
    """Why this routine must not fire right now, or None to go ahead. Ordered
    cheapest-first, and every branch returns copy the customer can act on."""
    if not routine.enabled:
        return "Routine is paused"
    if project.status in ("canceled", "finished"):
        return f"Project is {project.status}"
    if project.block_auto_development:
        return "Automatic development is blocked on this project"
    if routine.last_request_id:
        last = db.get(Request, routine.last_request_id)
        # The skip-while-open guard: without it a weekly routine stacks a second
        # build on last week's unmerged PR, then a third.
        if last is not None and last.status in OPEN_REQUEST_STATES:
            return "Previous run is still open"
    org = db.get(Organization, project.org_id)
    if org is None or (org.credit_balance or 0.0) <= 0:
        return "Not enough credits"
    if dev_concurrency.slots_full(db, project):
        return "A build is already running on this project"
    return None


def fire(db: Session, routine: ProjectRoutine, *, manual: bool = False) -> Request:
    """Create this routine's next Request. Raises RoutineError when a guard
    refuses. The caller commits and dispatches - keeping the DB write and the
    Celery send in the caller's hands is what lets the sweep batch and the API
    return the created request."""
    project = db.get(Project, routine.project_id)
    if project is None:
        raise RoutineError("Unknown project")
    if not enabled_sync(db):
        raise RoutineError("Routines are disabled on this instance")
    reason = _blocked_reason(db, routine, project)
    if reason:
        raise RoutineError(reason)

    req = Request(
        project_id=project.id, type="feature", handling="ai", status="open",
        title=routine.title[:TITLE_MAX],
        # §repo binding: an explicit pin when the routine names a repo, else the
        # push target resolves at dispatch exactly as for a typed request.
        repo_id=routine.repo_id or dev_concurrency.resolved_repo_id(db, project),
    )
    db.add(req)
    db.flush()
    routine.last_request_id = req.id
    routine.last_run_at = utcnow()
    routine.last_skip_reason = None
    if routine.schedule_cron:
        routine.next_run_at = next_run(routine.schedule_cron)
    log.info("routine %s fired request %s (manual=%s)", routine.id, req.id, manual)
    return req


def record_skip(routine: ProjectRoutine, reason: str) -> None:
    """Remember why a due tick did nothing and move the schedule on, so a
    blocked routine neither retries in a tight loop nor goes silent."""
    routine.last_skip_reason = reason[:255]
    if routine.schedule_cron:
        routine.next_run_at = next_run(routine.schedule_cron)


def out(routine: ProjectRoutine, last_status: str | None = None) -> dict:
    return {
        "id": routine.id, "project_id": routine.project_id, "title": routine.title,
        "prompt": routine.prompt, "enabled": routine.enabled,
        "schedule_cron": routine.schedule_cron, "next_run_at": routine.next_run_at,
        "last_run_at": routine.last_run_at, "last_request_id": routine.last_request_id,
        "last_request_status": last_status, "last_skip_reason": routine.last_skip_reason,
        "repo_id": routine.repo_id, "created_at": routine.created_at,
    }


def fire_now(routine_id: str) -> str:
    """Standalone sync twin for the async API (run_in_threadpool), mirroring
    dev_concurrency.acquire_for_project: opens its own session, fires, commits,
    returns the new request id. The caller dispatches the Celery task - after
    the commit, so the worker can never read a request that is not there yet."""
    from app.core.db import SyncSession
    from app.workers.tasks import _post_message

    with SyncSession() as db:
        routine = db.get(ProjectRoutine, routine_id)
        if routine is None:
            raise RoutineError("Unknown routine")
        req = fire(db, routine, manual=True)
        _post_message(db, routine.project_id, f"request:{req.id}", "customer",
                      routine.prompt)
        db.commit()
        return req.id
