"""§parallel-builds (docs/PARALLEL_BUILDS.md §2) - the concurrency entitlement
chokepoint. `effective_parallel_limit` is the ONLY place the per-project limit
is resolved (and `_entitlement_limit` the only line a future licensing/plan
layer touches); `acquire_slot` is the SOLE creator of DevRun ledger rows and
the gate every run_development sender passes through. MR1 keeps the gate DARK:
enforcement is hard-wired to limit 1 via today's Project.dev_run_state check
(provably identical behavior, see tests/test_dev_concurrency.py), while the
ledger rows it creates shadow the run so MR2/MR3 can start reading them.

Sync by design (the workers own dispatch); async API contexts call the
`*_for_project` helpers via run_in_threadpool (knowledge.py precedent) so the
chokepoint cannot fork into a second implementation.
"""
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SyncSession
from app.models import DevRun, Organization, Project, ProjectRepo, Request

log = logging.getLogger(__name__)

# Project.dev_run_state values that mean "a build is in flight" - the exact set
# today's handle_request gate refuses on.
INFLIGHT_SCALAR_STATES = ("running", "awaiting_merge", "deploying")
# DevRun.state values that hold a concurrency slot. 'superseded' is deliberately
# absent: it is the terminal state of a run whose open PR a revision took over
# (§revise), so its slot passes to the revision that continues its branch.
ACTIVE_ROW_STATES = ("queued", "running", "awaiting_merge", "deploying")

# Today's refusal copy, verbatim (provable-identity contract).
BUSY_DETAIL = ("A build is already in progress for this project - "
               "re-submit this request once it completes.")


def bind_run(project: Project, run: DevRun | None) -> None:
    """§parallel-builds MR2: carry the run through the synchronous run pipeline
    on the project instance itself (session-local plain attribute) - no
    signature churn, and unambiguous once siblings exist."""
    project._dev_run = run


def bound_run(project: Project) -> "DevRun | None":
    return getattr(project, "_dev_run", None)


def run_ws(project: Project, run: DevRun | None = None) -> Path:
    """THE sanctioned workspace join (docs/PARALLEL_BUILDS.md §5): a run with a
    workspace_dir lives under <workspaces>/<workspace_dir> OUTSIDE the project
    checkout; a legacy row ('') - and every run until MR3 - resolves to
    Project.workspace_path exactly as before. tests grep the run pipeline for
    direct joins, so never open workspace paths any other way there."""
    if run is None:
        run = bound_run(project)
    if run is not None and run.workspace_dir:
        return Path(settings.workspaces_dir) / run.workspace_dir
    return Path(project.workspace_path or "/nonexistent")


def runner_name(project: Project, run: DevRun | None = None) -> str:
    """Deployer container/Job name for a run: '' keeps the deployer's legacy
    dev-<project_id> naming; parallel-mode rows get a per-run suffix."""
    if run is None:
        run = bound_run(project)
    if run is not None and run.workspace_dir:
        return f"dev-{project.id}-{run.id[:8]}"
    return ""


class SlotRefused(Exception):
    """The gate refused a dispatch; str() is the customer-facing detail."""


def _entitlement_limit(db: Session, org: Organization | None) -> int | None:
    """The licensing hook (docs/PARALLEL_BUILDS.md §2): a future org/plan/license
    layer returns its cap here. None = no entitlement constraint."""
    return None


def effective_parallel_limit(db: Session, project: Project) -> int:
    """max(1, min(project override | instance default, instance max, entitlement))."""
    limit = project.dev_parallel_limit or settings.dev_parallel_runs_default
    limit = min(limit, settings.dev_parallel_runs_max)
    org = db.get(Organization, project.org_id)
    ent = _entitlement_limit(db, org)
    if ent is not None:
        limit = min(limit, ent)
    return max(1, limit)


def _mode_conflicts(active: list, parallel_mode: bool) -> bool:
    """Workspace-mode exclusivity: legacy rows own the project checkout,
    parallel rows live in isolated devruns dirs, and the two modes must never
    run on one working tree. One exemption: admitting a PARALLEL run past a
    legacy row parked in `awaiting_merge` is safe - that row's work is durably
    on the remote (awaiting_merge means branch pushed, PR open), no runner is
    executing for it, and `_refresh_root_workspace` rebuilds the canonical
    checkout fail-loud before its merge ever deploys - so an admin-raised limit
    takes effect instead of staying dead-lettered behind a human merge. The
    reverse stays strict: a legacy admission waits for EVERY active parallel
    row, because a sibling's merge hard-resets the canonical checkout the
    legacy run would be working in; and legacy rows in queued/running/deploying
    still block parallel admission - those are live users of the checkout."""
    for r in active:
        if bool(r.workspace_dir) == parallel_mode:
            continue
        if parallel_mode and not r.workspace_dir and r.state == "awaiting_merge":
            continue
        return True
    return False


def slots_full(db: Session, project: Project) -> bool:
    """True when a FRESH dispatch would not be admitted (the §12 chat gate and
    the auto_dev sweep guard; at limit 1 exactly the legacy scalar in-flight
    check). Mirrors acquire_slot's workspace-mode exclusivity (`_mode_conflicts`
    - gate and admission must agree or the sweep re-dispatches every minute
    into a refusal and its customer-facing busy copy)."""
    limit = effective_parallel_limit(db, project)
    if limit == 1 and project.dev_run_state in INFLIGHT_SCALAR_STATES:
        return True
    active = (db.query(DevRun)
              .filter(DevRun.project_id == project.id,
                      DevRun.state.in_(ACTIVE_ROW_STATES)).all())
    if len(active) >= limit:
        return True
    return _mode_conflicts(active, limit > 1)


def primary_run(db: Session, project: Project) -> "DevRun | None":
    """The run the Project.dev_* mirror tracks (§parallel-builds): the most
    recently STARTED active row, sticky to the newest-started row once every
    run finished. Feed readers bind it (bind_run) so run_ws re-roots them to
    the primary's workspace: a parallel-mode primary owns its own feed, while
    a legacy row ('') resolves to the project checkout exactly as before -
    without this, the live console replayed the previous legacy run's feed
    whenever the primary was parallel-mode (prod regression)."""
    q = (db.query(DevRun)
         .filter(DevRun.project_id == project.id,
                 DevRun.started_at.isnot(None)))
    active = (q.filter(DevRun.state.in_(ACTIVE_ROW_STATES))
              .order_by(DevRun.started_at.desc()).first())
    if active is not None:
        return active
    return q.order_by(DevRun.started_at.desc()).first()


def primary_for_project(project_id: str) -> "DevRun | None":
    """Standalone sync twin for async API callers (run_in_threadpool, the
    acquire_for_project precedent): opens its own session and returns the
    primary row detached (bind_run/run_ws only read its loaded attributes)."""
    with SyncSession() as db:
        project = db.get(Project, project_id)
        if project is None:
            return None
        row = primary_run(db, project)
        if row is not None:
            db.expunge(row)
        return row


def resolved_repo_id(db: Session, project: Project,
                     request: Request | None = None) -> str | None:
    """§repo binding: the connected repo a FRESH run for `request` builds into
    - the request's own intent (Request.repo_id) when set, else the project's
    default push target, else the first connected repo (mirroring
    _dev_target's live precedence). None = the platform GitLab repo (or no
    repo connected yet)."""
    if request is not None and request.repo_id:
        row = db.get(ProjectRepo, request.repo_id)
        if row is not None and row.project_id == project.id:
            return row.id
    rows = (db.query(ProjectRepo).filter_by(project_id=project.id)
            .order_by(ProjectRepo.role).all())
    push = next((r for r in rows if r.is_push_target), None)
    if push is not None:
        return push.id
    if project.gitlab_ssh_url and project.gitlab_project_id:
        return None  # the platform repo is the implicit target
    return rows[0].id if rows else None


def default_request(db: Session, project: Project) -> Request | None:
    """The request a non-scoped dispatch belongs to: the scoped request in
    flight, else Request #0, else None (legacy pre-threads project) - the same
    resolution _dev_thread narrates by."""
    if project.dev_request_id:
        return db.get(Request, project.dev_request_id)
    return (db.query(Request)
            .filter_by(project_id=project.id, type="mvp")
            .order_by(Request.created_at).first())


def acquire_slot(db: Session, project: Project, request: Request | None = None,
                 predecessor: DevRun | None = None) -> DevRun:
    """Take a run slot under SELECT..FOR UPDATE on the project row and create
    the DevRun ledger row (state 'queued'; run_development flips it to
    'running'). Raises SlotRefused with customer-facing copy. The caller owns
    the transaction (commit before dispatching the Celery task)."""
    db.execute(select(Project.id).where(Project.id == project.id).with_for_update())
    db.refresh(project)
    limit = effective_parallel_limit(db, project)
    active = (db.query(DevRun)
              .filter(DevRun.project_id == project.id,
                      DevRun.state.in_(ACTIVE_ROW_STATES)).all())
    if len(active) >= limit:
        raise SlotRefused(BUSY_DETAIL)
    if (limit == 1 and project.dev_run_state in INFLIGHT_SCALAR_STATES
            and not (predecessor is not None
                     and predecessor.state not in ACTIVE_ROW_STATES)):
        # Legacy belt at the serialized limit: a pre-ledger in-flight run has no
        # row, and two runs on ONE workspace would corrupt the checkout. A
        # TERMINAL predecessor is the exception (§revise): the caller just closed
        # out the very run this scalar mirrors, so the in-flight look is stale by
        # one statement - not a second run on the workspace.
        raise SlotRefused(BUSY_DETAIL)
    # Workspace-mode exclusivity: legacy rows own the project checkout, so a
    # parallel-mode admission must wait for them (and vice versa) - flipping
    # the limit mid-run can never mix modes on one working tree. Exemption and
    # rationale in _mode_conflicts.
    parallel_mode = limit > 1 and predecessor is None
    if predecessor is not None:
        parallel_mode = bool(predecessor.workspace_dir)
        if (limit > 1 and not predecessor.workspace_dir
                and predecessor.state == "superseded"):
            # §revise conversion: this chain continues a legacy run that was
            # parked awaiting_merge (release_for_revision just superseded it),
            # so its branch is pushed - a fresh isolated dir plus the runner
            # entrypoint's origin/<branch> continuation carries the work
            # without reclaiming the canonical checkout. Without this, revising
            # the old serialized PR would be refused for as long as any
            # parallel sibling is live. Failed legacy chains stay legacy: their
            # unpushed work lives only in the canonical checkout.
            parallel_mode = True
    if _mode_conflicts(active, parallel_mode):
        raise SlotRefused(BUSY_DETAIL)
    if request is None:
        request = default_request(db, project)
    if request is not None and any(r.request_id == request.id for r in active):
        raise SlotRefused("This request already has a build in flight.")
    floor = settings.dev_run_credit_floor
    if floor > 0:
        org = db.get(Organization, project.org_id)
        if (org.credit_balance or 0.0) < (len(active) + 1) * floor:
            raise SlotRefused("Your credit balance is too low to start another "
                              "build - top up to continue.")
    run = DevRun(project_id=project.id,
                 request_id=request.id if request is not None else None,
                 workspace_dir=predecessor.workspace_dir if predecessor else "",
                 branch=predecessor.branch if predecessor else None,
                 predecessor_id=predecessor.id if predecessor else None,
                 # §repo binding: a chain inherits its predecessor's pin
                 # VERBATIM (even null - _dev_target recovers a pre-binding
                 # chain's repo from its PR URL); only a fresh run resolves.
                 repo_id=(predecessor.repo_id if predecessor is not None
                          else resolved_repo_id(db, project, request)))
    db.add(run)
    db.flush()
    # A run chain keeps its predecessor's dir; a fresh parallel-mode run - or a
    # legacy revise chain converted above - gets its own isolated dir OUTSIDE
    # the project checkout (seeded at run start).
    if parallel_mode and not run.workspace_dir:
        run.workspace_dir = f"devruns/{project.id}/{run.id}"
        db.flush()
    return run


def acquire_for_project(project_id: str, request_id: str | None = None) -> str:
    """Standalone sync twin for async API callers (run_in_threadpool): opens its
    own session, commits the row, returns its id. Raises SlotRefused. A resume
    chains onto the request's latest failed run (same workspace dir + branch)."""
    with SyncSession() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise SlotRefused("Unknown project")
        request = db.get(Request, request_id) if request_id else None
        run = acquire_slot(db, project, request,
                           predecessor=latest_failed_run(db, project, request))
        db.commit()
        return run.id


def latest_failed_run(db: Session, project: Project,
                      request: Request | None = None) -> DevRun | None:
    """The run a resume continues (§run chains): the newest failed row for the
    resolved request - its workspace dir and branch carry the parked work."""
    if request is None:
        request = default_request(db, project)
    q = db.query(DevRun).filter(DevRun.project_id == project.id,
                                DevRun.state == "failed")
    if request is not None:
        q = q.filter(DevRun.request_id == request.id)
    return q.order_by(DevRun.created_at.desc()).first()


def release_for_revision(db: Session, project: Project,
                         request: Request | None = None) -> DevRun | None:
    """§revise: hand the awaiting-merge run's slot to the revision that
    continues it. The row is marked 'superseded' (terminal, outside
    ACTIVE_ROW_STATES) and returned as the predecessor, so acquire_slot admits
    the new run and it inherits the workspace dir + branch - the open pull
    request then picks up the revision's commits instead of a second PR opening
    beside it. Returns None when nothing awaits a merge. The caller commits.

    A fresh row (rather than re-running this one) is what keeps billing honest:
    `billed_through` is a per-row watermark over a cumulative usage.json, so a
    second session on the same row would meter as if it had already been paid."""
    if request is None:
        request = default_request(db, project)
    q = db.query(DevRun).filter(DevRun.project_id == project.id,
                                DevRun.state == "awaiting_merge")
    if request is not None:
        q = q.filter(DevRun.request_id == request.id)
    row = q.order_by(DevRun.created_at.desc()).first()
    if row is None:
        return None
    row.state = "superseded"
    return row


def adopt_or_create(db: Session, project: Project, run_id: str | None = None) -> DevRun:
    """run_development's row resolution: the acquired row by id, else the
    project's single active row (a message queued before its row's id was
    threaded through), else - the sanctioned bridge for messages queued across
    the deploy or senders not yet routed through acquire_slot - a fresh row."""
    if run_id:
        run = db.get(DevRun, run_id)
        if run is not None and run.project_id == project.id:
            return run
    run = (db.query(DevRun)
           .filter(DevRun.project_id == project.id,
                   DevRun.state.in_(ACTIVE_ROW_STATES))
           .order_by(DevRun.created_at.desc()).first())
    if run is not None:
        return run
    req = default_request(db, project)
    run = DevRun(project_id=project.id,
                 request_id=req.id if req is not None else None,
                 repo_id=resolved_repo_id(db, project, req))
    db.add(run)
    db.flush()
    return run
