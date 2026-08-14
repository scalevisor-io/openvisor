import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import dev_run_out, project_out, project_summary, share_out
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import (
    get_current_user, get_project_for_org_member, get_project_for_user,
    rate_limit, require_verified,
)
from app.core.encryption import decrypt, encrypt
from app.models import (
    DevRun,
    IssueWatchEvent, OnboardingAnswer, Organization, OrgMemory, Project,
    ProjectMemory, ProjectRepo, ProjectRoutine, ProjectShare,
    Request as RequestRow, StatusChange, User,
    utcnow,
)
from app.schemas.schemas import (
    AnswersIn, ProjectCreateIn, ProjectUpdateIn, RepoConnectIn, RepoUpdateIn,
    RoutineIn, RoutineUpdateIn, ShareIn,
)
from app.services import (
    app_settings, dev_concurrency, devfeed, model_prices, naming, project_actions,
    project_search, repos as repolib, routines as routines_svc, sshkeys, vision,
)
from app.services.pricing import load_static
from app.workers.celery_app import celery

router = APIRouter(prefix="/api/projects", tags=["projects"])


async def _shared_roles(db: AsyncSession, user: User) -> dict[str, str]:
    """§sharing: {project_id: role} for every project shared with this user."""
    rows = (await db.execute(select(ProjectShare).where(
        ProjectShare.user_id == user.id))).scalars().all()
    return {s.project_id: s.role for s in rows}


def _scoped(stmt, user: User, shared: dict[str, str]):
    """Visibility scope of the project list: the caller's org + shares."""
    return stmt.where(or_(Project.org_id == user.org_id,
                          Project.id.in_(list(shared))))


def _stamp_access(projects, user: User, shared: dict[str, str]) -> None:
    for p in projects:
        p.access_role = ("owner" if user.role == "admin" or p.org_id == user.org_id
                         else shared.get(p.id, "owner"))


@router.get("")
async def list_projects(all: int = 0, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    q = select(Project).order_by(Project.created_at.desc())
    shared: dict[str, str] = {}
    if not (user.role == "admin" and all):
        shared = await _shared_roles(db, user)
        q = _scoped(q, user, shared)
    projects = (await db.execute(q)).scalars().all()
    _stamp_access(projects, user, shared)
    return [project_summary(p) for p in projects]


async def _ai_search_budget(request: Request, user: User) -> bool:
    """Whether this org still has AI-rerank budget in the current 10-minute
    window. The rerank is free to the customer, so this cap - not billing - is
    what bounds the spend, and exceeding it must DEGRADE the search (text
    matching only) rather than 429 it: to the customer a failed request reads as
    the search box being broken."""
    if not settings.project_search_ai_enabled:
        return False
    try:
        await rate_limit(request, "project-search-ai", settings.project_search_rate_per_10min,
                         600, identity=f"org:{user.org_id}")
    except HTTPException as exc:
        if exc.status_code == 429:
            return False
        raise
    return True


@router.get("/search")
async def search_projects(request: Request, q: str = "", all: int = 0,
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """Rank the caller's projects against a natural-language query (§project
    search): deterministic text matching, then - within the per-org cap - an
    unbilled LLM rerank for intent-level queries. Same scope as `GET /projects`.
    `ai` reports which ranking came back, so the SPA can label the results; an
    empty query is the plain list. This route MUST stay declared before
    `/{project_id}`, or "search" is read as a project id."""
    await rate_limit(request, "project-search", 120, 60, identity=f"user:{user.id}")
    stmt = select(Project).order_by(Project.created_at.desc())
    shared: dict[str, str] = {}
    if not (user.role == "admin" and all):
        shared = await _shared_roles(db, user)
        stmt = _scoped(stmt, user, shared)
    projects = (await db.execute(stmt)).scalars().all()
    _stamp_access(projects, user, shared)
    by_id = {p.id: p for p in projects}
    query = (q or "").strip()
    if not query:
        return {"results": [project_summary(p) for p in projects], "ai": False, "reason": None}
    use_ai = await _ai_search_budget(request, user)
    records = [project_search.snapshot(p) for p in projects]
    ids, ai_used = await run_in_threadpool(project_search.search, records, query, use_ai)
    if ai_used:
        reason = None
    elif not settings.project_search_ai_enabled:
        reason = "disabled"
    elif not use_ai:
        reason = "rate_limited"
    else:
        reason = "unavailable"
    return {"results": [project_summary(by_id[i]) for i in ids if i in by_id],
            "ai": ai_used, "reason": reason}


@router.post("", status_code=201)
async def create_project(body: ProjectCreateIn, user: User = Depends(require_verified),
                         db: AsyncSession = Depends(get_db)):
    # Admin can pause new deposits of either kind (Admin settings). This gates
    # project *creation* only - requests/edits on existing projects are unaffected.
    # Admins themselves are exempt so they can still create while paused.
    if user.role != "admin":
        flags = await app_settings.get_deposit_pause(db)
        if app_settings.is_kind_paused(flags, body.kind):
            raise HTTPException(403, "deposits_paused")
    specs = {s["id"]: s for s in load_static("specialities.json")["specialities"] if s.get("enabled")}
    if body.kind in ("ai", "auto_dev"):
        if body.speciality not in specs:
            raise HTTPException(400, "Unknown speciality")
    elif body.speciality is not None and body.speciality not in specs:
        raise HTTPException(400, "Unknown speciality")
    if not body.from_scratch and not body.repos:
        raise HTTPException(400, "Provide at least one repository SSH URI, or start from scratch")
    if body.kind == "auto_dev":
        # The sentinel builds INTO the watched repo: a connected GitHub/GitLab push
        # repo is mandatory (its issues are polled; 'other' hosts have no issues API),
        # and at least one label or assignee trigger must be set.
        if not body.repos:
            raise HTTPException(400, "Connect the repository whose issues I should watch")
        if repolib.detect_provider(body.repos[0].ssh_uri) not in ("github", "gitlab"):
            raise HTTPException(400, "Issue watching supports GitHub and GitLab repositories only")
        if body.issue_watch is None or not (body.issue_watch.labels or body.issue_watch.assignees):
            raise HTTPException(400, "Set at least one label or assignee to watch")
    if body.kind == "chat" and body.repos:
        raise HTTPException(400, "Chat projects don't take repositories")

    # The customer doesn't type a name (§9.2): bootstrap one from the description
    # now; the evaluation task refines it with the LLM title prompt.
    project = Project(
        org_id=user.org_id, name=naming.name_from_description(body.description),
        kind=body.kind, speciality=body.speciality,
        description=body.description,
        from_scratch=body.from_scratch and body.kind != "auto_dev",
        sovereign=body.sovereign, sovereign_comment=body.sovereign_comment,
        block_auto_development=(body.kind in ("direct_quote", "chat")),
        issue_watch=body.issue_watch.model_dump() if body.kind == "auto_dev" else None,
    )
    # AI-built projects get the sandbox scaffolding (SSH key, subdomain, workspace,
    # demo creds); auto_dev needs the same (its per-issue builds are ordinary dev
    # runs). Direct-quote projects are managed manually - none of that applies.
    if body.kind in ("ai", "auto_dev"):
        private_pem, public_line = sshkeys.generate_keypair(f"{settings.brand_name} project")
        project.ssh_private_key_enc = encrypt(private_pem)
        project.ssh_public_key = public_line
    db.add(project)
    await db.flush()
    if body.kind in ("ai", "auto_dev"):
        project.subdomain = naming.subdomain_for(project.id, project.name)
        project.workspace_path = f"{settings.workspaces_dir}/{project.id}"
        project.demo_basic_auth_user = "demo"
        project.demo_basic_auth_pass_enc = encrypt(secrets.token_urlsafe(12))
    if body.kind == "ai":
        # §threads Request #0: the initial build is a request like any other, so
        # its whole build conversation lives in its own thread from day one.
        await project_actions.create_mvp_request(db, project)
    for i, repo in enumerate(body.repos):
        db.add(ProjectRepo(project_id=project.id, ssh_uri=repo.ssh_uri,
                           role="primary" if i == 0 else "secondary",
                           provider=repolib.detect_provider(repo.ssh_uri),
                           is_push_target=(i == 0)))
    if body.kind == "auto_dev":
        # §auto_dev skips evaluation/estimate/payment_due entirely: the sentinel is
        # born watching (usage-billed per run), so it starts in `development`.
        project.status = "development"
        db.add(StatusChange(project_id=project.id, from_status=None, to_status="development",
                            actor="customer", reason="Auto-developer project created"))
    elif body.kind == "chat":
        # §chat kind skips evaluation/estimate/payment_due the same way: the chat is
        # live immediately (opening fee + usage-billed answers), so it starts in
        # `development` - the conversational steady state.
        try:
            await project_actions.charge_chat_upfront(db, project)
        except project_actions.ActionError as exc:
            raise HTTPException(exc.status, exc.detail)
        project.status = "development"
        db.add(StatusChange(project_id=project.id, from_status=None, to_status="development",
                            actor="customer", reason="Chat opened"))
    else:
        db.add(StatusChange(project_id=project.id, from_status=None, to_status="draft",
                            actor="customer", reason="Project created"))
    await db.commit()
    await db.refresh(project, ["repos"])
    if body.kind == "ai":
        # async provisioning: GitLab user/project + workspace folder. auto_dev skips
        # it - the sentinel builds into its customer repo, no platform repo needed.
        celery.send_task("app.workers.tasks.provision_project", args=[project.id, user.email])
    elif body.kind == "chat":
        # The wizard's description IS the customer's opening message: seed the main
        # thread with it so the first answer starts without a second submit.
        try:
            await project_actions.post_chat_message(db, project, "customer", "main",
                                                    body.description)
        except project_actions.ActionError as exc:
            raise HTTPException(exc.status, exc.detail)
    return project_out(project)


@router.get("/{project_id}")
async def get_project(project: Project = Depends(get_project_for_user),
                      db: AsyncSession = Depends(get_db)):
    await db.refresh(project, ["repos"])
    # §chat images: stamped on the instance like `access_role`, so the pure
    # serializer stays session-free.
    project.image_support = await vision.project_image_support(db, project)
    # §parallel-builds MR4: the active run set behind the stacked consoles,
    # oldest started first (the SPA expands the newest - the mirror's primary).
    # Stamped like image_support so project_out stays session-free; list
    # payloads never carry it.
    active = (await db.execute(
        select(DevRun).where(DevRun.project_id == project.id,
                             DevRun.state.in_(dev_concurrency.ACTIVE_ROW_STATES))
        .order_by(DevRun.started_at.asc().nullslast(),
                  DevRun.created_at.asc()))).scalars().all()
    legacy_feed_owner = (await db.execute(
        select(DevRun.id).where(DevRun.project_id == project.id,
                                DevRun.workspace_dir == "",
                                DevRun.started_at.is_not(None))
        .order_by(DevRun.started_at.desc()).limit(1))).scalar_one_or_none()
    project.dev_runs_payload = [dev_run_out(r, project, legacy_feed_owner)
                                for r in active]
    # §repo binding: the live console's branch chip follows the PRIMARY run's
    # own pinned link (the mirror derivation only covers pin-less legacy rows).
    primary = next((r for r in reversed(project.dev_runs_payload)
                    if r["started_at"] is not None), None)
    if (primary is not None and primary["branch"]
            and primary["branch"] == project.dev_branch and primary["branch_url"]):
        project.dev_branch_url_pinned = primary["branch_url"]
    return project_out(project)


@router.patch("/{project_id}")
async def update_project(body: ProjectUpdateIn, project: Project = Depends(get_project_for_user),
                         db: AsyncSession = Depends(get_db)):
    if (body.name is None and body.description is None and body.issue_watch is None
            and body.git_author_name is None and body.git_author_email is None):
        raise HTTPException(400, "Provide a name, description, issue watch or git identity "
                                 "to update")
    if body.description is not None:
        # The description feeds evaluation and the dev pipeline; once the project
        # moves past review it becomes part of what was approved/priced, so it locks.
        # For auto_dev it IS the standing development policy - editable anytime.
        if project.kind != "auto_dev" and project.status not in ("draft", "awaiting_review"):
            raise HTTPException(409, "The description can only be edited while the project "
                                     "is in draft or awaiting review")
        project.description = body.description
    if body.issue_watch is not None:
        if project.kind != "auto_dev":
            raise HTTPException(409, "Issue watching applies to auto-developer projects only")
        if not (body.issue_watch.labels or body.issue_watch.assignees):
            raise HTTPException(400, "Set at least one label or assignee to watch")
        project.issue_watch = body.issue_watch.model_dump()
    if body.name is not None:
        # Renaming is cosmetic and allowed in any status; the demo subdomain keeps
        # its original slug so a deployed demo URL never moves.
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "The project name can't be empty")
        project.name = name
        project.name_customized = True
    # §git identity: an empty string is the documented reset to the instance
    # default, which is why these are stored as null rather than "".
    if body.git_author_name is not None:
        project.git_author_name = body.git_author_name.strip() or None
    if body.git_author_email is not None:
        project.git_author_email = body.git_author_email.strip() or None
    await db.commit()
    await db.refresh(project, ["repos"])
    return project_out(project)


# ---------------------------------------------------------------- repos (§multi-repo)

async def _project_repos(db: AsyncSession, project_id: str) -> list[ProjectRepo]:
    return (await db.execute(select(ProjectRepo).where(ProjectRepo.project_id == project_id)
                             .order_by(ProjectRepo.role))).scalars().all()


async def _resolve_repo_token(db: AsyncSession, project: Project, provider: str) -> str:
    """The PAT the auto-merge auth check (and the worker) use for this repo: the
    project's GITHUB_TOKEN/GITLAB_TOKEN Memory secret, else - GitHub only - the
    platform-wide token, else empty."""
    key = repolib.token_key(provider)
    if key:
        row = (await db.execute(select(ProjectMemory).where(
            ProjectMemory.project_id == project.id,
            ProjectMemory.key == key))).scalar_one_or_none()
        if row is None and await _project_uses_global_memory(db, project):
            # Fall back to a global (org-level) token when the project has none, so
            # the auto-merge auth check matches what the worker will actually use.
            row = (await db.execute(select(OrgMemory).where(
                OrgMemory.org_id == project.org_id,
                OrgMemory.key == key))).scalar_one_or_none()
        if row:
            val = decrypt(row.value_enc).strip()
            if val:
                return val
    return settings.github_token if provider == "github" else ""


async def _project_uses_global_memory(db: AsyncSession, project: Project) -> bool:
    """Effective per-project global-memory setting (override, else org default)."""
    if project.use_global_memory is not None:
        return project.use_global_memory
    org = await db.get(Organization, project.org_id)
    return bool(org.global_memory_enabled_default) if org else True


async def _get_repo(db: AsyncSession, project: Project, repo_id: str) -> ProjectRepo:
    repo = await db.get(ProjectRepo, repo_id)
    if repo is None or repo.project_id != project.id:
        raise HTTPException(404, "Repository not found")
    return repo


@router.post("/{project_id}/repos", status_code=201)
async def connect_repo(body: RepoConnectIn, project: Project = Depends(get_project_for_user),
                       db: AsyncSession = Depends(get_db)):
    """Connect one of the customer's own git repos (they add the project deploy key
    to it). The provider is detected from the URL host unless overridden. The first
    connected repo becomes the push target (switching away from the platform repo);
    later ones are added non-push. Returns the updated Project."""
    if project.kind not in ("ai", "auto_dev"):
        raise HTTPException(409, "Only AI projects build into a repository")
    ssh_uri = body.ssh_uri.strip()
    existing = await _project_repos(db, project.id)
    if any(r.ssh_uri == ssh_uri for r in existing):
        raise HTTPException(409, "That repository is already connected")
    provider = body.provider or repolib.detect_provider(ssh_uri)
    first = len(existing) == 0
    db.add(ProjectRepo(project_id=project.id, ssh_uri=ssh_uri,
                       role="primary" if first else "secondary",
                       provider=provider, is_push_target=first))
    await db.commit()
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.post("/{project_id}/repos/{repo_id}/verify-auth")
async def verify_repo_auth(repo_id: str, project: Project = Depends(get_project_for_user),
                           db: AsyncSession = Depends(get_db)):
    """Auth check gating auto-merge: a real authenticated API call confirming the
    project's GITHUB_TOKEN/GITLAB_TOKEN is valid AND can access THIS repo (GitHub:
    GET /repos/{owner}/{repo}; GitLab: GET /projects/:path). Returns {ok, detail}.
    'other' repos are never auto-mergeable, so ok is false with an explanation."""
    repo = await _get_repo(db, project, repo_id)
    if repo.provider not in repolib.AUTO_MERGE_PROVIDERS:
        return {"ok": False, "detail": "Auto-merge is only available for GitHub or GitLab repositories."}
    token = await _resolve_repo_token(db, project, repo.provider)
    ok, detail = repolib.check_auth(repo.provider, repo.ssh_uri, token)
    return {"ok": ok, "detail": detail}


@router.post("/{project_id}/repos/{repo_id}/verify-ssh")
async def verify_repo_ssh(repo_id: str, project: Project = Depends(get_project_for_user),
                          db: AsyncSession = Depends(get_db)):
    """SSH reachability check for a connected remote repo: `git ls-remote` over the
    project's deploy key confirms the host is reachable AND the key is authorized,
    up front, so a mis-added key surfaces here instead of failing the dev run at
    push time. Distinct from verify-auth (which checks the auto-merge PAT). Returns
    {ok, detail}; https/platform repos get a clear managed/skip note. Shells out, so
    it runs in a threadpool."""
    repo = await _get_repo(db, project, repo_id)
    key = decrypt(project.ssh_private_key_enc) if project.ssh_private_key_enc else ""
    ok, detail = await run_in_threadpool(repolib.check_ssh, repo.ssh_uri, key)
    return {"ok": ok, "detail": detail}


@router.patch("/{project_id}/repos/{repo_id}")
async def update_repo(body: RepoUpdateIn, repo_id: str,
                      project: Project = Depends(get_project_for_user),
                      db: AsyncSession = Depends(get_db)):
    """Set this repo as the push target (exactly one - the others are cleared; all
    false means the platform repo is the push target) and/or its auto_merge. Turning
    auto_merge ON re-runs the auth check server-side and is rejected (409) unless it
    passes - never trust the client. Returns the updated Project."""
    if project.kind not in ("ai", "auto_dev"):
        raise HTTPException(409, "Only AI projects build into a repository")
    repo = await _get_repo(db, project, repo_id)
    if body.is_push_target is not None:
        if body.is_push_target:
            for r in await _project_repos(db, project.id):
                r.is_push_target = (r.id == repo.id)
        else:
            repo.is_push_target = False
    if body.auto_merge is not None:
        if body.auto_merge:
            if repo.provider not in repolib.AUTO_MERGE_PROVIDERS:
                raise HTTPException(422, "Auto-merge is only available for GitHub or GitLab repositories.")
            token = await _resolve_repo_token(db, project, repo.provider)
            ok, detail = repolib.check_auth(repo.provider, repo.ssh_uri, token)
            if not ok:
                raise HTTPException(409, f"Auto-merge can't be enabled: {detail}")
        repo.auto_merge = body.auto_merge
    if body.squash_on_merge is not None:
        repo.squash_on_merge = body.squash_on_merge
    if body.summarize_to_issue is not None:
        repo.summarize_to_issue = body.summarize_to_issue
    await db.commit()
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.delete("/{project_id}/repos/{repo_id}")
async def remove_repo(repo_id: str, project: Project = Depends(get_project_for_user),
                      db: AsyncSession = Depends(get_db)):
    """Disconnect a repo. If it was the push target, the platform repo (or the next
    connected repo) takes over. Returns the updated Project."""
    repo = await _get_repo(db, project, repo_id)
    was_push = repo.is_push_target
    await db.delete(repo)
    await db.flush()
    if was_push:
        # Promote the first remaining connected repo (else the platform repo, which
        # is push whenever none of the connected repos is).
        remaining = await _project_repos(db, project.id)
        if remaining:
            remaining[0].is_push_target = True
    await db.commit()
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.post("/{project_id}/repos/use-platform")
async def use_platform_repo(project: Project = Depends(get_project_for_user),
                            db: AsyncSession = Depends(get_db)):
    """Make the platform-auto-generated GitLab repo the push target again by
    clearing every connected repo's push flag. Returns the updated Project."""
    if project.kind not in ("ai", "auto_dev"):
        raise HTTPException(409, "Only AI projects build into a repository")
    for r in await _project_repos(db, project.id):
        r.is_push_target = False
    await db.commit()
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.post("/{project_id}/answers")
async def save_answers(body: AnswersIn, project: Project = Depends(get_project_for_user),
                       db: AsyncSession = Depends(get_db)):
    # Answers feed the evaluation/estimate, so they lock once the project leaves
    # draft. auto_dev never has an estimate and is born in `development` - like
    # its description, the answers are standing dev-run context, editable anytime.
    if project.kind != "auto_dev" and project.status != "draft":
        raise HTTPException(409, "Answers can only be edited in draft")
    known = {q["id"] for q in load_static("initial-user-questions.json")["questions"]}
    for a in body.answers:
        if a.question_id not in known:
            raise HTTPException(400, f"Unknown question {a.question_id}")
        existing = (await db.execute(select(OnboardingAnswer).where(
            OnboardingAnswer.project_id == project.id,
            OnboardingAnswer.question_id == a.question_id))).scalar_one_or_none()
        payload = {"option_ids": a.option_ids, "comment": a.comment}
        if existing:
            existing.answer = payload
        else:
            db.add(OnboardingAnswer(project_id=project.id, question_id=a.question_id,
                                    answer=payload))
    await db.commit()
    return {"ok": True}


@router.post("/{project_id}/evaluate")
async def evaluate(project: Project = Depends(get_project_for_user),
                   db: AsyncSession = Depends(get_db)):
    try:
        task_id = await project_actions.evaluate(db, project)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return {"task_id": task_id}


@router.get("/{project_id}/evaluation")
async def get_evaluation(project: Project = Depends(get_project_for_user)):
    ev = project.evaluation or {"state": "none"}
    return ev


@router.post("/{project_id}/submit")
async def submit(project: Project = Depends(get_project_for_user),
                 db: AsyncSession = Depends(get_db)):
    try:
        await project_actions.submit(db, project)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.post("/{project_id}/require-review")
async def require_review(project: Project = Depends(get_project_for_user),
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    """Pull the consultant into an AI project. Charges REVIEW_REQUEST_CREDITS (refundable
    by the admin). Customer-only: the admin is the reviewer and moves projects
    through the admin status control instead."""
    if user.role == "admin":
        raise HTTPException(403, "Admins can't request their own review - customers use "
                                 f"this to hand the project to {settings.consultant_first_name}.")
    try:
        await project_actions.require_review(db, project)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.post("/{project_id}/retry-build")
async def retry_build(project: Project = Depends(get_project_for_user),
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """Resume development after a build stalled, timed out, or failed (§14.5).
    Gated by the same dev_resume_capability the UI renders, so the button and
    the endpoint can't drift: there must be an actual failed run, no run in
    flight, a repository to build into, and the project must not be closed
    (finished/canceled - a finished project takes new work via requests)."""
    try:
        await project_actions.retry_build(db, project, is_admin=user.role == "admin")
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.post("/{project_id}/stop-build")
async def stop_build(run_id: str | None = None,
                     project: Project = Depends(get_project_for_user)):
    """§14: stop the in-flight build. The worker kills the sandboxed runner and
    parks the run as a normal failed, resumable one ("Stopped at your request") -
    progress already pushed to the branch is kept. Only the agent-build phase is
    stoppable; deploying/awaiting_merge belong to other owners. §parallel-builds
    MR4: ?run_id= scopes the stop to one sibling console's run."""
    try:
        project_actions.stop_build(project, run_id)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    return {"ok": True}


@router.post("/{project_id}/approve-delivery")
async def approve_delivery(project: Project = Depends(get_project_for_user),
                           user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    """The customer accepts the delivered MVP once the demo is live, moving the
    project to `finished` (§ delivery acceptance). Allowed from awaiting_customer
    for the customer or admin."""
    actor = "admin" if user.role == "admin" else "customer"
    try:
        await project_actions.approve_delivery(db, project, actor)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.get("/{project_id}/dev-logs")
async def dev_logs(run_id: str | None = None,
                   project: Project = Depends(get_project_for_user),
                   db: AsyncSession = Depends(get_db)):
    """Last dev-run log tail + sub-state, for the build panel / failure diagnosis.
    ?run_id= serves one ledger row's captured log instead (the request-thread
    run-history consoles)."""
    if run_id:
        row = await db.get(DevRun, run_id)
        if row is None or row.project_id != project.id:
            raise HTTPException(404, "Unknown run")
        return {
            "dev_run_state": row.state,
            "dev_run_error": row.run_error,
            "dev_run_started_at": row.started_at,
            "dev_harness_version": row.harness_version,
            "dev_pr_number": row.pr_number,
            "dev_pr_url": row.pr_url,
            "log": row.run_log or "",
        }
    return {
        "dev_run_state": project.dev_run_state,
        "dev_run_error": project.dev_run_error,
        "dev_run_started_at": project.dev_run_started_at,
        "dev_harness_version": project.dev_harness_version,
        "dev_pr_number": project.dev_pr_number,
        "dev_pr_url": project.dev_pr_url,
        "log": project.dev_run_log or "",
    }


@router.get("/{project_id}/dev-activity")
async def dev_activity(offset: int = 0, run_id: str | None = None,
                       project: Project = Depends(get_project_for_user),
                       db: AsyncSession = Depends(get_db)):
    """Live build console (§14.8): offset-polled sanitized agent-activity
    events + the runner's token snapshot, served straight off the workspaces
    volume (Programs §28 log parity - file reads run in a threadpool).
    §parallel-builds MR2: ?run_id= selects one run's feed (its workspace paths);
    absent = the PRIMARY run - the mirror's most-recently-started active row,
    sticky when finished - so a parallel-mode primary serves its own feed
    instead of replaying the stale legacy file (a legacy primary still resolves
    to the project path, byte-identical at limit 1). The offset shrink/reset
    contract lets the polling client restart its buffer across the switch."""
    if run_id:
        row = await db.get(DevRun, run_id)
        if row is None or row.project_id != project.id:
            raise HTTPException(404, "Unknown run")
        dev_concurrency.bind_run(project, row)
    else:
        row = await run_in_threadpool(dev_concurrency.primary_for_project, project.id)
        if row is not None:
            dev_concurrency.bind_run(project, row)
    # Resolved here because the feed read runs in a threadpool with no session:
    # a model the static price table doesn't know is still billable through its
    # endpoint, so the live estimate prices it exactly as the worker bills it.
    prices = await model_prices.all_prices(db)
    return await run_in_threadpool(devfeed.read_chunk, project, offset, prices)


@router.get("/{project_id}/dev-runs")
async def dev_runs(request_id: str | None = None,
                   project: Project = Depends(get_project_for_user),
                   db: AsyncSession = Depends(get_db)):
    """§threads: the development history behind a request's thread - one DevRun
    ledger row per run, newest first. `has_feed` marks the rows whose activity
    feed dev-activity?run_id= can still serve: a parallel-mode run owns its
    workspace feed for as long as the workspace lives, while legacy rows share
    the project workspace feed, which belongs to the newest started one."""
    q = select(DevRun).where(DevRun.project_id == project.id)
    if request_id:
        q = q.where(DevRun.request_id == request_id)
    rows = (await db.execute(
        q.order_by(DevRun.created_at.desc(), DevRun.id.desc()))).scalars().all()
    legacy_feed_owner = (await db.execute(
        select(DevRun.id).where(DevRun.project_id == project.id,
                                DevRun.workspace_dir == "",
                                DevRun.started_at.is_not(None))
        .order_by(DevRun.started_at.desc()).limit(1))).scalar_one_or_none()
    await db.refresh(project, ["repos"])
    return [dev_run_out(r, project, legacy_feed_owner) for r in rows]


@router.get("/{project_id}/issue-events")
async def issue_events(limit: int = 20, offset: int = 0,
                       project: Project = Depends(get_project_for_user),
                       db: AsyncSession = Depends(get_db)):
    """§auto_dev: the Issue-watch card's paginated intake history - what the
    sweep did with each received issue (registered/deferred/paused/started)."""
    if project.kind != "auto_dev":
        raise HTTPException(409, "auto_dev projects only")
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    total = (await db.execute(
        select(func.count()).select_from(IssueWatchEvent)
        .where(IssueWatchEvent.project_id == project.id))).scalar_one()
    rows = (await db.execute(
        select(IssueWatchEvent).where(IssueWatchEvent.project_id == project.id)
        .order_by(IssueWatchEvent.created_at.desc(), IssueWatchEvent.id.desc())
        .offset(offset).limit(limit))).scalars().all()
    return {"total": total, "events": [
        {"id": e.id, "kind": e.kind, "issue_url": e.issue_url,
         "issue_title": e.issue_title, "request_id": e.request_id,
         "detail": e.detail, "created_at": e.created_at} for e in rows]}


@router.get("/{project_id}/status-history")
async def status_history(project: Project = Depends(get_project_for_user),
                         db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(StatusChange).where(
        StatusChange.project_id == project.id).order_by(StatusChange.at))).scalars().all()
    return [{"from": r.from_status, "to": r.to_status, "actor": r.actor,
             "reason": r.reason, "at": r.at} for r in rows]


# ---- §sharing: give a registered user contributor/read-only access ----

@router.get("/{project_id}/shares")
async def list_shares(project: Project = Depends(get_project_for_org_member),
                      db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ProjectShare, User).join(User, ProjectShare.user_id == User.id)
        .where(ProjectShare.project_id == project.id)
        .order_by(ProjectShare.created_at))).all()
    return [share_out(s, u) for s, u in rows]


@router.post("/{project_id}/shares", status_code=201)
async def add_share(body: ShareIn, request: Request,
                    project: Project = Depends(get_project_for_org_member),
                    user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    """Create-or-update: sharing again with the same user just changes the role.
    The target sees the project immediately - no invitation, no email.
    Rate-limited per USER (counted before the lookup, misses included): the
    404-vs-success answer necessarily confirms whether an email is registered,
    so the cap is what keeps this useless as a bulk email-enumeration oracle."""
    await rate_limit(request, "share-add", settings.share_rate_per_hour, 3600,
                     identity=f"user:{user.id}")
    email = body.email.strip().lower()
    target = (await db.execute(select(User).where(
        func.lower(User.email) == email))).scalars().first()
    if target is None:
        raise HTTPException(404, "No account with this email on the platform")
    if target.role == "admin":
        raise HTTPException(400, "Admins already see every project")
    if target.org_id == project.org_id:
        raise HTTPException(400, "This user already has access through the project's organization")
    share = (await db.execute(select(ProjectShare).where(
        ProjectShare.project_id == project.id,
        ProjectShare.user_id == target.id))).scalar_one_or_none()
    if share is None:
        share = ProjectShare(project_id=project.id, user_id=target.id,
                             role=body.role, created_by=user.id)
        db.add(share)
    else:
        share.role = body.role
    await db.commit()
    return share_out(share, target)


@router.delete("/{project_id}/shares/{share_id}")
async def remove_share(share_id: str,
                       project: Project = Depends(get_project_for_org_member),
                       db: AsyncSession = Depends(get_db)):
    share = await db.get(ProjectShare, share_id)
    if share is None or share.project_id != project.id:
        raise HTTPException(404, "Unknown share")
    await db.delete(share)
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- §routines

async def _routines_enabled_or_409(db: AsyncSession) -> None:
    """The instance kill switch, enforced server-side on every write. The SPA
    also hides the tab from `GET /meta/config`, but that is advisory - this is
    the gate, and it is what a future paid tier flips."""
    if await app_settings.get_flag(db, routines_svc.ROUTINES_DISABLED):
        raise HTTPException(403, "Routines are disabled on this instance")


async def _get_routine(db: AsyncSession, project: Project, routine_id: str) -> ProjectRoutine:
    routine = await db.get(ProjectRoutine, routine_id)
    if routine is None or routine.project_id != project.id:
        raise HTTPException(404, "Unknown routine")
    return routine


async def _routine_out(db: AsyncSession, routine: ProjectRoutine) -> dict:
    """Serialize with the last spawned request's status, which is what the
    skip-while-open guard keys on - the customer sees the same fact the sweep
    decides on."""
    status = None
    if routine.last_request_id:
        last = await db.get(RequestRow, routine.last_request_id)
        status = last.status if last is not None else None
    return routines_svc.out(routine, status)


def _validated_cron_or_400(cron: str) -> str:
    cron = (cron or "").strip()
    if cron:
        error = routines_svc.validate_cron(cron)
        if error:
            raise HTTPException(400, error)
    return cron


async def _validated_repo_or_400(db: AsyncSession, project: Project,
                                 repo_id: str | None) -> str | None:
    if not repo_id:
        return None
    repo = await db.get(ProjectRepo, repo_id)
    if repo is None or repo.project_id != project.id:
        raise HTTPException(400, "Unknown repository for this project")
    return repo.id


@router.get("/{project_id}/routines")
async def list_routines(project: Project = Depends(get_project_for_user),
                        db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ProjectRoutine).where(ProjectRoutine.project_id == project.id)
        .order_by(ProjectRoutine.created_at))).scalars().all()
    return [await _routine_out(db, r) for r in rows]


@router.post("/{project_id}/routines")
async def create_routine(body: RoutineIn, project: Project = Depends(get_project_for_user),
                         db: AsyncSession = Depends(get_db)):
    await _routines_enabled_or_409(db)
    cron = _validated_cron_or_400(body.schedule_cron)
    routine = ProjectRoutine(
        project_id=project.id, title=body.title.strip(), prompt=body.prompt.strip(),
        enabled=body.enabled, schedule_cron=cron,
        repo_id=await _validated_repo_or_400(db, project, body.repo_id),
        next_run_at=routines_svc.next_run(cron) if (cron and body.enabled) else None)
    db.add(routine)
    await db.commit()
    return await _routine_out(db, routine)


@router.put("/{project_id}/routines/{routine_id}")
async def update_routine(routine_id: str, body: RoutineUpdateIn,
                         project: Project = Depends(get_project_for_user),
                         db: AsyncSession = Depends(get_db)):
    await _routines_enabled_or_409(db)
    routine = await _get_routine(db, project, routine_id)
    if body.title is not None:
        routine.title = body.title.strip()
    if body.prompt is not None:
        routine.prompt = body.prompt.strip()
    if body.schedule_cron is not None:
        routine.schedule_cron = _validated_cron_or_400(body.schedule_cron)
    if body.repo_id is not None:
        routine.repo_id = await _validated_repo_or_400(db, project, body.repo_id or None)
    if body.enabled is not None:
        routine.enabled = body.enabled
    # One rule for the next occurrence however the row was edited: a scheduled,
    # enabled routine always has a fresh one; anything else has none.
    routine.next_run_at = (routines_svc.next_run(routine.schedule_cron)
                           if routine.schedule_cron and routine.enabled else None)
    routine.updated_at = utcnow()
    await db.commit()
    return await _routine_out(db, routine)


@router.delete("/{project_id}/routines/{routine_id}")
async def delete_routine(routine_id: str, project: Project = Depends(get_project_for_user),
                         db: AsyncSession = Depends(get_db)):
    routine = await _get_routine(db, project, routine_id)
    await db.delete(routine)
    await db.commit()
    return {"ok": True}


@router.post("/{project_id}/routines/{routine_id}/run")
async def run_routine(routine_id: str, project: Project = Depends(get_project_for_user),
                      db: AsyncSession = Depends(get_db)):
    """Fire now, ignoring the schedule but NOT the guards - the same
    `routines.fire` the sweep calls, so "Run now" can never do something the
    scheduled path would have refused."""
    await _routines_enabled_or_409(db)
    routine = await _get_routine(db, project, routine_id)
    try:
        request_id = await run_in_threadpool(routines_svc.fire_now, routine.id)
    except routines_svc.RoutineError as exc:
        raise HTTPException(409, str(exc))
    await db.refresh(routine)
    celery.send_task("app.workers.tasks.handle_request",
                     args=[project.id, request_id, ""])
    return await _routine_out(db, routine)
