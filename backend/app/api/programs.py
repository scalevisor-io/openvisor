"""Programs (§28): the customer catalog/instances/runs API and the admin CRUD.

Run logs and output files are served straight off the shared workspaces volume
(the deployer streams run.log there live; the worker copies output/ there after
the run), so "live logs" is just offset-polling this API. Blocked runs (§28
leak scan) withhold both; live chunks additionally have the platform secret
values redacted server-side - the post-run scan is the hard gate, this closes
the watch-it-live window for the cheap substring case."""
import json
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, require_admin
from app.core.encryption import decrypt, encrypt
from app.models import (
    ModelEndpoint, Organization, Program, ProgramInstance, ProgramRun, User, utcnow,
)
from app.schemas.schemas import (
    ProgramCreateIn, ProgramInstanceCreateIn, ProgramInstanceUpdateIn,
    ProgramUpdateIn,
)
from app.services import gitlab, leakscan, sshkeys
from app.services import programs as programs_svc
from app.workers.celery_app import celery

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["programs"])
admin_router = APIRouter(prefix="/api/admin/programs", tags=["admin-programs"],
                         dependencies=[Depends(require_admin)])

ACTIVE_STATES = ("queued", "running")
LOG_CHUNK_BYTES = 65536


# ---------------------------------------------------------------- serializers

def _program_brief(p: Program) -> dict:
    return {"id": p.id, "title": p.title, "short_description": p.short_description,
            "schedulable": p.schedulable, "is_published": p.is_published,
            "created_at": p.created_at}


def _program_public(p: Program) -> dict:
    return {**_program_brief(p), "readme_md": p.readme_md,
            "input_template": p.input_template or []}


def _program_admin(p: Program) -> dict:
    return {**_program_public(p),
            "gitlab_repo_path": p.gitlab_repo_path,
            "gitlab_web_url": p.gitlab_web_url,
            "default_branch": p.default_branch,
            "model_endpoint_id": p.model_endpoint_id,
            # True while a pre-endpoint inline config still resolves (cleared the
            # moment an endpoint or the global default is picked) - the UI shows a
            # hint so the admin knows what the fallback is doing.
            "has_legacy_model_config": bool(p.model_endpoint_id is None and (
                p.openai_base_url or p.openai_api_key_enc or p.model_name)),
            "credit_markup": p.credit_markup,
            "credit_markup_effective": (settings.credit_markup if p.credit_markup is None
                                        else p.credit_markup),
            "timeout_minutes": p.timeout_minutes,
            "cpu_request": p.cpu_request, "cpu_limit": p.cpu_limit,
            "mem_request": p.mem_request, "mem_limit": p.mem_limit,
            "last_check_state": p.last_check_state,
            "last_check_at": p.last_check_at,
            "last_check_run_id": p.last_check_run_id,
            "updated_at": p.updated_at}


def _run_out(r: ProgramRun, full: bool = False) -> dict:
    out = {"id": r.id, "instance_id": r.instance_id, "kind": r.kind, "state": r.state,
           "exit_code": r.exit_code, "error": r.error,
           "tokens_input": r.tokens_input, "tokens_output": r.tokens_output,
           "cost_credits": r.cost_credits, "webhook_status": r.webhook_status,
           "created_at": r.created_at, "started_at": r.started_at,
           "finished_at": r.finished_at}
    if full:
        out |= {"output_text": r.output_text, "output_files": r.output_files or [],
                "log_tail": r.log_tail}
    return out


def _instance_out(inst: ProgramInstance, program: Program,
                  latest: ProgramRun | None) -> dict:
    """Input values are returned in clear (Memory parity: they stay encrypted
    at rest; template `secret` fields drive display masking only)."""
    return {"id": inst.id, "program": _program_public(program), "label": inst.label,
            "inputs": programs_svc.load_inputs(inst),
            # §28 per-instance model: null = run on the program's admin-set default.
            "model_endpoint_id": inst.model_endpoint_id,
            "webhook_url": inst.webhook_url,
            "hook_enabled": inst.hook_enabled,
            # The receiver URL + shared secret the customer pastes into their
            # repo's webhook settings. Owner-scoped route (like Memory values,
            # returned in clear; the UI masks visually).
            "hook_url": f"{settings.app_base_url}/api/programs/hooks/{inst.id}",
            "hook_secret": decrypt(inst.hook_secret_enc) if inst.hook_secret_enc else None,
            "hook_filters": inst.hook_filters or {"actions": [], "labels": [],
                                                  "assignees": [], "authors": []},
            "schedule_enabled": inst.schedule_enabled,
            "schedule_cron": inst.schedule_cron,
            "next_run_at": inst.next_run_at,
            "ssh_public_key": inst.ssh_public_key,
            "created_at": inst.created_at,
            "latest_run": _run_out(latest) if latest else None}


# ---------------------------------------------------------------- shared helpers

async def get_instance_for_user(
    instance_id: str, user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProgramInstance:
    inst = await db.get(ProgramInstance, instance_id)
    if inst is None or (user.role != "admin" and inst.org_id != user.org_id):
        raise HTTPException(404, "Program instance not found")
    return inst


async def _latest_run(db: AsyncSession, instance_id: str) -> ProgramRun | None:
    return (await db.execute(
        select(ProgramRun).where(ProgramRun.instance_id == instance_id)
        .order_by(ProgramRun.created_at.desc()).limit(1))).scalars().first()


async def _active_run_exists(db: AsyncSession, instance_id: str) -> bool:
    row = await db.execute(select(ProgramRun.id).where(
        ProgramRun.instance_id == instance_id,
        ProgramRun.state.in_(ACTIVE_STATES)).limit(1))
    return row.first() is not None


async def _run_endpoints(db: AsyncSession, program: Program,
                         instance: ProgramInstance | None) -> list[ModelEndpoint]:
    """The saved endpoints a run of this program+instance could resolve through
    (instance pick, then program default), fetched for the log-redaction refuse
    set - see programs_svc.program_model_keys for why it is a superset."""
    eps = [await db.get(ModelEndpoint, eid)
           for eid in programs_svc.model_endpoint_ids(program, instance)]
    return [ep for ep in eps if ep is not None]


def _log_chunk(run: ProgramRun, program: Program,
               instance: ProgramInstance | None, offset: int,
               endpoints: list[ModelEndpoint] | None = None) -> dict:
    """One live-log chunk from the run's file on the workspaces volume. Blocked
    runs get the stored findings summary instead of the raw log; live content
    has the platform secret values redacted (cheap substring pass - the
    post-run scan remains the hard gate)."""
    finished = run.state not in ACTIVE_STATES
    if run.state == "blocked":
        return {"content": run.log_tail or "", "next_offset": 0, "done": True}
    path = programs_svc.run_dir(run.program_id, run.instance_id, run.id) / "run.log"
    try:
        size = path.stat().st_size
    except OSError:
        # pruned (old run) or not started writing yet - fall back to the DB tail
        return {"content": run.log_tail or "", "next_offset": 0, "done": finished}
    offset = max(0, min(offset, size))
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(LOG_CHUNK_BYTES)
    content = data.decode("utf-8", "replace")
    try:
        secrets = leakscan.platform_secret_values(
            extra_values=programs_svc.program_model_keys(program, endpoints or []),
            ssh_private_keys=([decrypt(instance.ssh_private_key_enc)]
                              if instance is not None else []))
        for value in secrets:
            content = content.replace(value, "[redacted]")
    except Exception:  # noqa: BLE001 - redaction is best-effort
        pass
    return {"content": content, "next_offset": offset + len(data),
            "done": finished and offset + len(data) >= size}


async def _validated_inputs_or_422(program: Program, values: dict) -> dict:
    fields = program.input_template or []
    errors, _resolved = programs_svc.validate_inputs(fields, values)
    if errors:
        raise HTTPException(422, {"message": "invalid inputs", "errors": errors})
    return values


# ---------------------------------------------------------------- customer: catalog

@router.get("/programs")
async def list_programs(q: str = "", user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """Published catalog, searchable by title or short description (admins
    also see unpublished)."""
    stmt = select(Program).order_by(Program.title).limit(100)
    if user.role != "admin":
        stmt = stmt.where(Program.is_published.is_(True))
    if q.strip():
        needle = f"%{q.strip()}%"
        stmt = stmt.where(or_(Program.title.ilike(needle),
                              Program.short_description.ilike(needle)))
    rows = (await db.execute(stmt)).scalars().all()
    return [_program_brief(p) for p in rows]


@router.get("/programs/{program_id}")
async def get_program(program_id: str, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    program = await db.get(Program, program_id)
    if program is None or (user.role != "admin" and not program.is_published):
        raise HTTPException(404, "Program not found")
    return _program_public(program)


@router.post("/programs/{program_id}/instances")
async def create_instance(program_id: str, body: ProgramInstanceCreateIn,
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    program = await db.get(Program, program_id)
    if program is None or (user.role != "admin" and not program.is_published):
        raise HTTPException(404, "Program not found")
    private_key, public_key = sshkeys.generate_keypair(
        f"openvisor-program-{program.id[:8]}")
    inst = ProgramInstance(
        id=str(uuid.uuid4()), program_id=program.id, org_id=user.org_id,
        label=body.label.strip(), ssh_public_key=public_key,
        ssh_private_key_enc=encrypt(private_key),
        hook_secret_enc=encrypt(secrets.token_urlsafe(24)))
    db.add(inst)
    await db.commit()
    return _instance_out(inst, program, None)


# ---------------------------------------------------------------- customer: instances

@router.get("/program-model-endpoints")
async def list_selectable_endpoints(user: User = Depends(get_current_user),
                                    db: AsyncSession = Depends(get_db)):
    """The saved ModelEndpoints a customer may pin a program instance to (§28
    per-instance model). Deliberately narrow: id + label + model name only -
    never the base URL, the key or the prices. The labels ARE admin-authored
    text that becomes customer-visible here, and an endpoint with no model set
    is not selectable (nothing could resolve through it)."""
    eps = (await db.execute(
        select(ModelEndpoint).where(ModelEndpoint.model_name.isnot(None))
        .order_by(ModelEndpoint.label))).scalars().all()
    return [{"id": ep.id, "label": ep.label, "model_name": ep.model_name}
            for ep in eps]


@router.get("/program-instances")
async def list_instances(user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    stmt = select(ProgramInstance).order_by(ProgramInstance.created_at.desc())
    if user.role != "admin":
        stmt = stmt.where(ProgramInstance.org_id == user.org_id)
    instances = (await db.execute(stmt)).scalars().all()
    out = []
    for inst in instances:
        program = await db.get(Program, inst.program_id)
        out.append(_instance_out(inst, program, await _latest_run(db, inst.id)))
    return out


@router.get("/program-instances/{instance_id}")
async def get_instance(inst: ProgramInstance = Depends(get_instance_for_user),
                       db: AsyncSession = Depends(get_db)):
    program = await db.get(Program, inst.program_id)
    return _instance_out(inst, program, await _latest_run(db, inst.id))


@router.put("/program-instances/{instance_id}")
async def update_instance(body: ProgramInstanceUpdateIn,
                          inst: ProgramInstance = Depends(get_instance_for_user),
                          db: AsyncSession = Depends(get_db)):
    program = await db.get(Program, inst.program_id)
    if body.label is not None:
        inst.label = body.label.strip()
    if body.inputs is not None:
        await _validated_inputs_or_422(program, body.inputs)
        inst.inputs_enc = encrypt(json.dumps(body.inputs))
    if body.model_endpoint_id is not None:
        # §28 per-instance model: "" clears the pick back to the program default.
        # An endpoint with no model set is not selectable, so it 404s like a
        # missing one rather than reporting the admin's config state.
        choice = body.model_endpoint_id.strip()
        if choice:
            ep = await db.get(ModelEndpoint, choice)
            if ep is None or not ep.model_name:
                raise HTTPException(404, "Model endpoint not found")
        inst.model_endpoint_id = choice or None
    if body.webhook_url is not None:
        url = body.webhook_url.strip()
        error = await run_in_threadpool(programs_svc.validate_webhook_url, url)
        if error:
            raise HTTPException(400, error)
        inst.webhook_url = url
    if body.schedule_cron is not None:
        error = programs_svc.validate_cron(body.schedule_cron) if body.schedule_cron.strip() else None
        if error:
            raise HTTPException(400, error)
        inst.schedule_cron = body.schedule_cron.strip()
    if body.schedule_enabled is not None:
        if body.schedule_enabled and not program.schedulable:
            raise HTTPException(400, "This program cannot be scheduled")
        inst.schedule_enabled = body.schedule_enabled
    if body.hook_filters is not None:
        inst.hook_filters = body.hook_filters.model_dump()
    if body.hook_enabled is not None:
        # Pre-hook instances have no secret yet - generate one on first enable.
        if body.hook_enabled and not inst.hook_secret_enc:
            inst.hook_secret_enc = encrypt(secrets.token_urlsafe(24))
        inst.hook_enabled = body.hook_enabled
    # consistency: an enabled schedule always has a valid cron and a fresh
    # next occurrence (covers enabling, and editing/clearing the cron while on)
    if inst.schedule_enabled:
        error = programs_svc.validate_cron(inst.schedule_cron)
        if error:
            raise HTTPException(400, error)
        inst.next_run_at = programs_svc.next_run(inst.schedule_cron)
    else:
        inst.next_run_at = None
    inst.updated_at = utcnow()
    await db.commit()
    return _instance_out(inst, program, await _latest_run(db, inst.id))


@router.delete("/program-instances/{instance_id}")
async def delete_instance(inst: ProgramInstance = Depends(get_instance_for_user),
                          db: AsyncSession = Depends(get_db)):
    if await _active_run_exists(db, inst.id):
        raise HTTPException(409, "A run is in progress - wait for it to finish")
    program_id, instance_id = inst.program_id, inst.id
    await db.execute(delete(ProgramRun).where(ProgramRun.instance_id == inst.id))
    await db.delete(inst)
    await db.commit()
    celery.send_task("app.workers.programs.cleanup_program_sandbox",
                     args=[program_id, instance_id])
    return {"ok": True}


@router.post("/program-instances/{instance_id}/hook-secret")
async def rotate_hook_secret(inst: ProgramInstance = Depends(get_instance_for_user),
                             db: AsyncSession = Depends(get_db)):
    """Rotate the inbound-hook secret (§28): invalidates every previously
    configured webhook immediately - the customer re-pastes the new value."""
    secret = secrets.token_urlsafe(24)
    inst.hook_secret_enc = encrypt(secret)
    inst.updated_at = utcnow()
    await db.commit()
    return {"hook_secret": secret}


@router.post("/program-instances/{instance_id}/run")
async def run_instance(inst: ProgramInstance = Depends(get_instance_for_user),
                       db: AsyncSession = Depends(get_db)):
    """Manual trigger. One run per instance at a time (409), wallet must be
    positive (402), inputs must validate against the cached template (422 -
    the run re-validates against the fresh repo anyway)."""
    # serialize concurrent clicks on the instance row
    inst = (await db.execute(select(ProgramInstance)
                             .where(ProgramInstance.id == inst.id)
                             .with_for_update())).scalar_one()
    program = await db.get(Program, inst.program_id)
    if await _active_run_exists(db, inst.id):
        raise HTTPException(409, "A run is already in progress")
    org = await db.get(Organization, inst.org_id)
    if (org.credit_balance or 0.0) <= 0:
        raise HTTPException(402, "Insufficient credits")
    await _validated_inputs_or_422(program, programs_svc.load_inputs(inst))
    run = ProgramRun(id=str(uuid.uuid4()), program_id=program.id,
                     instance_id=inst.id, org_id=inst.org_id, kind="manual")
    db.add(run)
    await db.commit()
    celery.send_task("app.workers.programs.run_program", args=[run.id])
    return _run_out(run)


# ---------------------------------------------------------------- customer: runs

@router.get("/program-instances/{instance_id}/runs")
async def list_runs(limit: int = 50,
                    inst: ProgramInstance = Depends(get_instance_for_user),
                    db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ProgramRun).where(ProgramRun.instance_id == inst.id)
        .order_by(ProgramRun.created_at.desc())
        .limit(max(1, min(limit, 200))))).scalars().all()
    return [_run_out(r) for r in rows]


async def _get_run(db: AsyncSession, inst: ProgramInstance, run_id: str) -> ProgramRun:
    run = await db.get(ProgramRun, run_id)
    if run is None or run.instance_id != inst.id:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/program-instances/{instance_id}/runs/{run_id}")
async def get_run(run_id: str, inst: ProgramInstance = Depends(get_instance_for_user),
                  db: AsyncSession = Depends(get_db)):
    run = await _get_run(db, inst, run_id)
    return _run_out(run, full=True)


@router.get("/program-instances/{instance_id}/runs/{run_id}/log")
async def get_run_log(run_id: str, offset: int = 0,
                      inst: ProgramInstance = Depends(get_instance_for_user),
                      db: AsyncSession = Depends(get_db)):
    run = await _get_run(db, inst, run_id)
    program = await db.get(Program, inst.program_id)
    eps = await _run_endpoints(db, program, inst)
    return await run_in_threadpool(_log_chunk, run, program, inst, offset, eps)


@router.get("/program-instances/{instance_id}/runs/{run_id}/files/{path:path}")
async def download_run_file(run_id: str, path: str,
                            inst: ProgramInstance = Depends(get_instance_for_user),
                            db: AsyncSession = Depends(get_db)):
    run = await _get_run(db, inst, run_id)
    if run.state == "blocked":
        raise HTTPException(403, "Output withheld by the leak scan")
    base = (programs_svc.run_dir(run.program_id, run.instance_id, run.id)
            / "output").resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "File not found (old runs are pruned)")
    filename = target.name.replace('"', "")
    return FileResponse(target, filename=filename)


# ---------------------------------------------------------------- admin CRUD

async def _get_program_admin(db: AsyncSession, program_id: str) -> Program:
    program = await db.get(Program, program_id)
    if program is None:
        raise HTTPException(404, "Program not found")
    return program


def _fetch_repo_meta(path: str) -> dict:
    """Resolve the repo and read README.md + input.template.yml (sync, run in a
    threadpool). Raises GitLabError/TemplateError with admin-facing messages."""
    gl = gitlab.get_project_by_path(path)
    ref = gl.get("default_branch") or "main"
    readme = gitlab.read_raw_file(gl["id"], "README.md", ref=ref) or ""
    template_text = gitlab.read_raw_file(gl["id"], programs_svc.TEMPLATE_FILE, ref=ref)
    fields = (programs_svc.parse_input_template(template_text)
              if template_text is not None else [])
    return {"id": gl["id"], "web_url": gl.get("web_url"), "default_branch": ref,
            "readme": readme, "fields": fields}


@admin_router.get("")
async def admin_list_programs(db: AsyncSession = Depends(get_db)):
    programs = (await db.execute(
        select(Program).order_by(Program.created_at.desc()))).scalars().all()
    counts = dict((await db.execute(
        select(ProgramInstance.program_id, func.count())
        .group_by(ProgramInstance.program_id))).all())
    return [{**_program_admin(p), "instances_count": counts.get(p.id, 0)}
            for p in programs]


@admin_router.post("")
async def admin_create_program(body: ProgramCreateIn, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(Program.id).where(
        Program.gitlab_repo_path == body.gitlab_repo_path))).first()
    if existing:
        raise HTTPException(409, "A program already uses this repository")
    try:
        meta = await run_in_threadpool(_fetch_repo_meta, body.gitlab_repo_path)
    except gitlab.GitLabError as exc:
        raise HTTPException(400, str(exc))
    except programs_svc.TemplateError as exc:
        raise HTTPException(400, f"repo rejected: {exc}")
    program = Program(
        title=body.title.strip(), short_description=body.short_description.strip(),
        gitlab_repo_path=body.gitlab_repo_path, gitlab_project_id=meta["id"],
        gitlab_web_url=meta["web_url"], default_branch=meta["default_branch"],
        readme_md=meta["readme"], input_template=meta["fields"])
    db.add(program)
    await db.commit()
    return _program_admin(program)


@admin_router.get("/{program_id}")
async def admin_get_program(program_id: str, db: AsyncSession = Depends(get_db)):
    program = await _get_program_admin(db, program_id)
    count = (await db.execute(select(func.count()).select_from(ProgramInstance)
                              .where(ProgramInstance.program_id == program.id))).scalar()
    return {**_program_admin(program), "instances_count": count or 0}


@admin_router.put("/{program_id}")
async def admin_update_program(program_id: str, body: ProgramUpdateIn,
                               db: AsyncSession = Depends(get_db)):
    program = await _get_program_admin(db, program_id)
    if body.title is not None:
        program.title = body.title.strip()
    if body.short_description is not None:
        program.short_description = body.short_description.strip()
    if body.default_branch is not None:
        program.default_branch = body.default_branch.strip()
    if body.is_published is not None:
        program.is_published = body.is_published
    if body.schedulable is not None:
        program.schedulable = body.schedulable
    if "model_endpoint_id" in body.model_fields_set:
        if body.model_endpoint_id:
            ep = await db.get(ModelEndpoint, body.model_endpoint_id)
            if ep is None:
                raise HTTPException(404, "Model endpoint not found")
            if not ep.model_name:
                raise HTTPException(400, "This endpoint has no model selected - edit it "
                                         "under Model configuration first")
            program.model_endpoint_id = ep.id
        else:
            program.model_endpoint_id = None
        # Picking an endpoint (or the global default) supersedes any legacy
        # inline config - clear it so the fallback can never shadow the choice.
        program.openai_base_url = None
        program.openai_api_key_enc = None
        program.model_name = None
    if body.credit_markup is not None:
        program.credit_markup = body.credit_markup
    if body.timeout_minutes is not None:
        program.timeout_minutes = body.timeout_minutes
    for field in ("cpu_request", "cpu_limit", "mem_request", "mem_limit"):
        value = getattr(body, field)
        if value is not None:
            setattr(program, field, value)
    program.updated_at = utcnow()
    await db.commit()
    return _program_admin(program)


@admin_router.post("/{program_id}/refresh")
async def admin_refresh_program(program_id: str, db: AsyncSession = Depends(get_db)):
    """Re-fetch README.md + input.template.yml from the repo (§28: the repo is
    the source of truth; runs also re-sync the template on every execution)."""
    program = await _get_program_admin(db, program_id)
    try:
        meta = await run_in_threadpool(_fetch_repo_meta, program.gitlab_repo_path)
    except gitlab.GitLabError as exc:
        raise HTTPException(400, str(exc))
    except programs_svc.TemplateError as exc:
        raise HTTPException(400, f"refresh rejected: {exc}")
    program.gitlab_project_id = meta["id"]
    program.gitlab_web_url = meta["web_url"]
    program.readme_md = meta["readme"]
    program.input_template = meta["fields"]
    program.updated_at = utcnow()
    await db.commit()
    return _program_admin(program)


@admin_router.post("/{program_id}/check")
async def admin_check_program(program_id: str, db: AsyncSession = Depends(get_db)):
    """"Check Program run" (§28): dry run with template defaults in the check
    sandbox. Passes iff docker build + compose deploy succeed - the program's
    own exit code (even an error) is acceptable dry-run output."""
    program = await _get_program_admin(db, program_id)
    active = (await db.execute(select(ProgramRun.id).where(
        ProgramRun.program_id == program.id, ProgramRun.kind == "check",
        ProgramRun.state.in_(ACTIVE_STATES)).limit(1))).first()
    if active:
        raise HTTPException(409, "A check run is already in progress")
    run = ProgramRun(id=str(uuid.uuid4()), program_id=program.id, kind="check")
    db.add(run)
    await db.commit()
    celery.send_task("app.workers.programs.run_program", args=[run.id])
    return _run_out(run)


@admin_router.get("/{program_id}/runs")
async def admin_list_runs(program_id: str, limit: int = 50,
                          db: AsyncSession = Depends(get_db)):
    program = await _get_program_admin(db, program_id)
    rows = (await db.execute(
        select(ProgramRun).where(ProgramRun.program_id == program.id)
        .order_by(ProgramRun.created_at.desc())
        .limit(max(1, min(limit, 200))))).scalars().all()
    return [_run_out(r) for r in rows]


@admin_router.get("/{program_id}/runs/{run_id}")
async def admin_get_run(program_id: str, run_id: str, db: AsyncSession = Depends(get_db)):
    program = await _get_program_admin(db, program_id)
    run = await db.get(ProgramRun, run_id)
    if run is None or run.program_id != program.id:
        raise HTTPException(404, "Run not found")
    return _run_out(run, full=True)


@admin_router.get("/{program_id}/runs/{run_id}/log")
async def admin_run_log(program_id: str, run_id: str, offset: int = 0,
                        db: AsyncSession = Depends(get_db)):
    program = await _get_program_admin(db, program_id)
    run = await db.get(ProgramRun, run_id)
    if run is None or run.program_id != program.id:
        raise HTTPException(404, "Run not found")
    inst = await db.get(ProgramInstance, run.instance_id) if run.instance_id else None
    eps = await _run_endpoints(db, program, inst)
    return await run_in_threadpool(_log_chunk, run, program, inst, offset, eps)


@admin_router.delete("/{program_id}")
async def admin_delete_program(program_id: str, db: AsyncSession = Depends(get_db)):
    program = await _get_program_admin(db, program_id)
    count = (await db.execute(select(func.count()).select_from(ProgramInstance)
                              .where(ProgramInstance.program_id == program.id))).scalar()
    if count:
        raise HTTPException(409, f"{count} customer instance(s) exist - delete them first "
                                 "or unpublish the program instead")
    await db.execute(delete(ProgramRun).where(ProgramRun.program_id == program.id))
    await db.delete(program)
    await db.commit()
    celery.send_task("app.workers.programs.cleanup_program_sandbox",
                     args=[program_id, None])
    return {"ok": True}
