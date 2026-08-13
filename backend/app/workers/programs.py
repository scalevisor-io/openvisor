"""Celery tasks for Programs (§28): execute one run in a throwaway DinD via the
deployer, bill its usage.json against the org wallet, leak-scan the output,
deliver the webhook, sweep due schedules, and clean up deleted instances.

A run's artifacts live under <workspaces>/programs/<instance|check-<pid>>/runs/<run>/:
run.log (streamed live by the deployer, served live by the API), output/ (copied
out of the sandbox), usage.json (billing report, consumed here). The staged
sandbox content (work/: repo + input/ + secrets/ + .openvisor/) is deleted after
every run - it holds the instance SSH key and the platform model keys."""
import json
import logging
import shutil
import time
from datetime import timedelta
from pathlib import Path

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SyncSession
from app.core.encryption import decrypt
from app.models import ModelEndpoint, Organization, Program, ProgramInstance, ProgramRun, utcnow
from app.services import brand, deployer_client, leakscan
from app.services import programs as programs_svc
from app.services.gitlab import GitLabError, download_archive
from app.services.llm import record_org_usage
from app.services.pricing import UnknownModelError
from app.services.sshkeys import generate_keypair
from app.workers.celery_app import celery

log = logging.getLogger(__name__)

OUTPUT_TEXT_CAP = 256 * 1024  # output.txt chars kept on the run row
OUTPUT_FILES_CAP = 500  # listed files per run
LOG_TAIL_CAP = 16000  # run-row log tail (dev_run_log parity)


def _wlog(rdir: Path, text: str) -> None:
    """Worker-side lines in the same run.log the deployer streams into."""
    with (rdir / "run.log").open("a", encoding="utf-8") as f:
        f.write(text)


def _read_log(rdir: Path, cap: int = 2_000_000) -> str:
    p = rdir / "run.log"
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > cap:
                f.seek(-cap, 2)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _out_of_credits(db: Session, org_id: str) -> bool:
    org = db.get(Organization, org_id)
    return org is None or (org.credit_balance or 0.0) <= 0


# ---------------------------------------------------------------- run execution

@celery.task(name="app.workers.programs.run_program")
def run_program(run_id: str) -> None:
    with SyncSession() as db:
        run = db.get(ProgramRun, run_id)
        if run is None or run.state != "queued":
            return  # already handled (double dispatch is harmless)
        if run.instance_id is not None:
            other = db.execute(select(ProgramRun.id).where(
                ProgramRun.instance_id == run.instance_id,
                ProgramRun.state == "running",
                ProgramRun.id != run.id).limit(1)).first()
            if other is not None:
                # One run per instance at a time (§28.3). Hook bursts enqueue
                # eagerly; a busy instance leaves the run queued and the sweep's
                # deferred pass re-dispatches it once the instance is free.
                return
        program = db.get(Program, run.program_id)
        instance = db.get(ProgramInstance, run.instance_id) if run.instance_id else None
        if program is None or (run.instance_id is not None and instance is None):
            run.state, run.error = "failed", "program or instance no longer exists"
            run.finished_at = utcnow()
            db.commit()
            return
        run.state = "running"
        run.started_at = utcnow()
        db.commit()
        try:
            _execute(db, run, program, instance)
        except Exception as exc:  # noqa: BLE001 - never leave a run stuck "running"
            log.exception("program run %s crashed", run_id)
            run.state = "failed"
            run.error = f"internal error: {exc}"[:512]
        run.finished_at = utcnow()
        _fire_webhook(run, program, instance)
        db.commit()


def _execute(db: Session, run: ProgramRun, program: Program,
             instance: ProgramInstance | None) -> None:
    instance_id = instance.id if instance is not None else None
    rdir = programs_svc.run_dir(program.id, instance_id, run.id)
    shutil.rmtree(rdir, ignore_errors=True)
    rdir.mkdir(parents=True, exist_ok=True)
    work = rdir / "work"
    try:
        if not _prepare(db, run, program, instance, rdir, work):
            return  # run already marked failed with the reason in run.log
        if run.org_id is not None and _out_of_credits(db, run.org_id):
            run.state, run.error = "failed", "insufficient credits"
            _wlog(rdir, "Refusing to start: the organization wallet is empty.\n")
            return
        result = None
        try:
            result = deployer_client.run_program(
                programs_svc.sandbox_name(program.id, instance_id),
                programs_svc.rel_run_dir(program.id, instance_id, run.id),
                timeout_s=program.timeout_minutes * 60,
                cpu_limit=program.cpu_limit, mem_limit=program.mem_limit,
                cpu_request=program.cpu_request, mem_request=program.mem_request)
        except deployer_client.DeployerError as exc:
            run.state, run.error = "failed", str(exc)[:512]
            _wlog(rdir, f"\n===== deployer error =====\n{exc}\n")
        _bill(db, run, program, rdir)
        if result is not None:
            _apply_result(run, program, result)
        _finalize_outputs(db, run, program, instance, rdir)
        if instance is not None:
            programs_svc.prune_runs(programs_svc.instance_dir(program.id, instance.id),
                                    settings.program_run_retention)
    finally:
        # work/ holds the instance SSH key and the platform model keys - gone
        # the moment the sandbox no longer needs it, success or not.
        shutil.rmtree(work, ignore_errors=True)


def _prepare(db: Session, run: ProgramRun, program: Program,
             instance: ProgramInstance | None, rdir: Path, work: Path) -> bool:
    """Materialize the sandbox content: repo archive + input/input.yml +
    secrets/ssh_key + .openvisor/program.env. Returns False (with the run marked
    failed and the reason in run.log) on any §28 pre-flight failure."""
    _wlog(rdir, f"Preparing run of '{program.title}' "
                f"(repo {program.gitlab_repo_path}, ref {program.default_branch})\n")
    if not program.gitlab_project_id:
        run.state, run.error = "failed", "program repo not resolved on GitLab"
        _wlog(rdir, "The program has no resolved GitLab project id.\n")
        return False
    try:
        download_archive(program.gitlab_project_id, str(work), ref=program.default_branch)
    except GitLabError as exc:
        run.state, run.error = "failed", f"program repo unavailable: {exc}"[:512]
        _wlog(rdir, f"Could not fetch the program repository: {exc}\n")
        return False

    template_path = work / programs_svc.TEMPLATE_FILE
    fields: list[dict] = []
    if template_path.is_file():
        try:
            fields = programs_svc.parse_input_template(template_path.read_text())
        except programs_svc.TemplateError as exc:
            run.state, run.error = "failed", f"invalid {programs_svc.TEMPLATE_FILE}: {exc}"[:512]
            _wlog(rdir, f"Invalid {programs_svc.TEMPLATE_FILE}: {exc}\n")
            return False
    else:
        _wlog(rdir, f"No {programs_svc.TEMPLATE_FILE} in the repo - the program takes no inputs.\n")
    # The repo is the source of truth: keep the cached form definition in sync
    # on every run so a template change shows up without an admin refresh.
    program.input_template = fields

    values = programs_svc.load_inputs(instance) if instance is not None else {}
    errors, resolved = programs_svc.validate_inputs(fields, values)
    if errors and run.kind != "check":
        run.state, run.error = "failed", "invalid inputs - fix them in the instance configuration"
        _wlog(rdir, "Input validation failed:\n" + "".join(
            f"  - {name}: {msg}\n" for name, msg in sorted(errors.items())))
        return False
    if errors:  # check run: defaults only; report what a customer would have to fill
        _wlog(rdir, "Check run uses template defaults; unset inputs:\n" + "".join(
            f"  - {name}: {msg}\n" for name, msg in sorted(errors.items())))

    (work / "input").mkdir(exist_ok=True)
    (work / "input" / "input.yml").write_text(
        yaml.safe_dump(resolved, sort_keys=True, allow_unicode=True))
    if run.hook_event:
        # §28 hooks: the normalized inbound event that triggered this run rides
        # along as input/event.json (additive - template programs may ignore it).
        (work / "input" / "event.json").write_text(
            json.dumps(run.hook_event, indent=2, ensure_ascii=False))
    secrets_dir = work / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    key_path = secrets_dir / "ssh_key"
    if instance is not None:
        key_path.write_text(decrypt(instance.ssh_private_key_enc))
    else:
        # checks still need the file (the template compose declares the secret);
        # a throwaway key keeps the real instance keys out of admin dry runs
        key_path.write_text(generate_keypair(f"progchk-{program.id}")[0])
    key_path.chmod(0o600)
    programs_svc.write_program_env(db, work, program, instance)
    return True


def _apply_result(run: ProgramRun, program: Program, result: dict) -> None:
    run.exit_code = str(result.get("exit_code"))
    timed_out = bool(result.get("timed_out"))
    build_ok = bool(result.get("build_ok"))
    deploy_ok = bool(result.get("deploy_ok"))
    if run.kind == "check":
        # §28 check criterion: docker build + deploy must succeed; the program's
        # own exit code (even an error) is acceptable output for a dry run.
        passed = build_ok and deploy_ok and not timed_out
        run.state = "succeeded" if passed else ("timeout" if timed_out else "failed")
        if timed_out:
            run.error = f"run exceeded the {program.timeout_minutes} minute timeout"
        elif not passed:
            run.error = "docker build failed" if not build_ok else "compose deploy failed"
        program.last_check_state = "passed" if passed else "failed"
        program.last_check_at = utcnow()
        program.last_check_run_id = run.id
        return
    if timed_out:
        run.state = "timeout"
        run.error = f"run exceeded the {program.timeout_minutes} minute timeout"
    elif not build_ok:
        run.state, run.error = "failed", f"docker build failed (exit {run.exit_code})"
    elif not deploy_ok:
        run.state, run.error = "failed", f"compose deploy failed (exit {run.exit_code})"
    elif run.exit_code != "0":
        run.state, run.error = "failed", f"program exited with code {run.exit_code}"
    else:
        run.state = "succeeded"


def _bill(db: Session, run: ProgramRun, program: Program, rdir: Path) -> None:
    """§28 billing, dev-run parity: the program wrote .openvisor/usage.json
    ({model, input_tokens, output_tokens}), the deployer copied it next to the
    run dir, and it is billed here at the PROGRAM's markup via the same
    record_org_usage path as MCP queries. Missing report = nothing billed
    (documented tradeoff, same as a timeout-killed dev run); unknown model
    fails loud to the admin instead of billing 0 (OCPA rule). Check runs have
    no org to bill - their report is discarded."""
    path = rdir / "usage.json"
    if not path.is_file():
        return
    try:
        usage = json.loads(path.read_text())
        path.unlink(missing_ok=True)  # never bill the same run twice
        if run.org_id is None or not isinstance(usage, dict):
            return
        usage = {"model": str(usage.get("model") or ""),
                 "input_tokens": int(usage.get("input_tokens") or 0),
                 "output_tokens": int(usage.get("output_tokens") or 0),
                 "cached_input_tokens": int(usage.get("cached_input_tokens") or 0)}
        if not (usage["input_tokens"] or usage["output_tokens"]):
            return
        credits = record_org_usage(
            db, run.org_id, [usage],
            f"program run {run.id.split('-')[0]} - {program.title[:60]}",
            kind="program_run", markup=program.credit_markup)
        run.tokens_input = usage["input_tokens"]
        run.tokens_output = usage["output_tokens"]
        run.cost_credits = credits
        log.info("program run %s billed: %d tokens, %.4f credits", run.id,
                 usage["input_tokens"] + usage["output_tokens"], credits)
    except UnknownModelError as exc:
        celery.send_task("app.workers.tasks.send_email", args=[
            settings.admin_email,
            brand.subject(f"Program run usage could not be billed: {program.title}"),
            f"Run {run.id} of program '{program.title}' reported usage for an "
            f"unpriced model ({exc}). Add the model to the price table - the "
            "tokens were NOT billed."])
    except Exception as exc:  # noqa: BLE001
        log.warning("program-run billing skipped for %s: %s", run.id, exc)


def _collect_outputs(rdir: Path) -> tuple[str | None, list[dict]]:
    out_dir = rdir / "output"
    if not out_dir.is_dir():
        return None, []
    files: list[dict] = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file() or p.name == ".gitkeep":  # repo scaffolding, not output
            continue
        try:
            files.append({"path": str(p.relative_to(out_dir)), "size": p.stat().st_size})
        except OSError:
            continue
        if len(files) >= OUTPUT_FILES_CAP:
            break
    text = None
    txt = out_dir / "output.txt"
    if txt.is_file():
        try:
            text = txt.read_text(encoding="utf-8", errors="replace")[:OUTPUT_TEXT_CAP]
        except OSError:
            pass
    return text, files


def _finalize_outputs(db: Session, run: ProgramRun, program: Program,
                      instance: ProgramInstance | None, rdir: Path) -> None:
    """Collect output.txt + the file list, then (customer runs only) apply the
    §28 leak scan to everything about to become visible - output files AND the
    run log, both of which reach the customer and the webhook. Fail OPEN on an
    internal scanner error (defence in depth); fail CLOSED on findings."""
    text, files = _collect_outputs(rdir)
    run.output_text = text
    run.output_files = files
    log_text = _read_log(rdir)
    if run.kind != "check":
        findings: list[str] = []
        try:
            eps = [db.get(ModelEndpoint, eid)
                   for eid in programs_svc.model_endpoint_ids(program, instance)]
            secrets = leakscan.platform_secret_values(
                extra_values=programs_svc.program_model_keys(program, eps),
                ssh_private_keys=([decrypt(instance.ssh_private_key_enc)]
                                  if instance is not None else []))
            fingerprints = leakscan.kb_fingerprints_from_db()
            abs_files = [rdir / "output" / f["path"] for f in files]
            findings = leakscan.scan_output(rdir, abs_files, log_text, secrets, fingerprints)
        except Exception as exc:  # noqa: BLE001 - fail open on internal errors
            log.warning("program output scan errored for %s (allowing): %s", run.id, exc)
        if findings:
            _block_run(run, program, findings)
            return
    run.log_tail = log_text[-LOG_TAIL_CAP:] if log_text else None


def _block_run(run: ProgramRun, program: Program, findings: list[str]) -> None:
    run.state = "blocked"
    run.error = "output withheld - the leak scan matched confidential material"
    run.output_text = None
    run.output_files = None
    run.log_tail = ("Output withheld (§28 leak scan). Findings:\n"
                    + "\n".join(f"  - {f}" for f in findings))
    celery.send_task("app.workers.tasks.send_email", args=[
        settings.admin_email,
        brand.subject(f"Program run blocked: {program.title}"),
        f"Run {run.id} of program '{program.title}' produced output matching "
        "confidential material (platform secrets or knowledge-base text). The "
        "output and log were withheld from the customer and the webhook.\n\n"
        + "\n".join(f"- {f}" for f in findings)])


def _fire_webhook(run: ProgramRun, program: Program,
                  instance: ProgramInstance | None) -> None:
    """POST the outcome to the instance webhook at the end of EVERY run -
    success or failure - carrying the generated text or the error message.
    Best-effort: 3 attempts with short backoff, 10s timeout each, delivery
    status on the run row. Never raises (the run outcome is already final)."""
    if instance is None or not (instance.webhook_url or "").strip():
        return
    payload = {
        "run_id": run.id,
        "instance_id": instance.id,
        "instance_label": instance.label,
        "program": {"id": program.id, "title": program.title},
        "kind": run.kind,
        "state": run.state,
        "exit_code": run.exit_code,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "credits_charged": run.cost_credits or 0.0,
        "output": run.output_text,
    }
    for attempt in range(3):
        try:
            resp = httpx.post(instance.webhook_url, json=payload, timeout=10)
            if resp.status_code < 300:
                run.webhook_status = "delivered"
                return
            log.warning("webhook for run %s: HTTP %s (attempt %d/3)",
                        run.id, resp.status_code, attempt + 1)
        except httpx.HTTPError as exc:
            log.warning("webhook for run %s failed (attempt %d/3): %s",
                        run.id, attempt + 1, exc)
        time.sleep(2 * (attempt + 1))
    run.webhook_status = "failed"


# ---------------------------------------------------------------- schedule sweep

@celery.task(name="app.workers.programs.program_schedule_sweep")
def program_schedule_sweep() -> None:
    """Beat, every minute (§28): start due scheduled runs (one per instance at
    a time - a due tick with a run in flight just reschedules), disable the
    schedule of an empty wallet with a visible failed run, dispatch deferred
    hook runs once their instance frees up, re-dispatch runs a lost enqueue
    left "queued", and reap runs a dead worker left "running"."""
    now = utcnow()
    dispatch: list[str] = []
    hooks: list[tuple[ProgramRun, Program, ProgramInstance]] = []
    with SyncSession() as db:
        _sweep_due(db, now, dispatch, hooks)
        _dispatch_deferred(db, dispatch, now)
        _requeue_lost(db, dispatch, now)
        _reap_stale_runs(db, hooks, now)
        db.commit()
        for run, program, inst in hooks:
            _fire_webhook(run, program, inst)
        db.commit()  # persist webhook_status
    for run_id in dict.fromkeys(dispatch):  # dedupe, keep order
        celery.send_task("app.workers.programs.run_program", args=[run_id])


def _sweep_due(db: Session, now, dispatch: list[str], hooks: list) -> None:
    due = db.execute(
        select(ProgramInstance)
        .where(ProgramInstance.schedule_enabled.is_(True),
               ProgramInstance.next_run_at.is_not(None),
               ProgramInstance.next_run_at <= now)
        .with_for_update(skip_locked=True)).scalars().all()
    for inst in due:
        program = db.get(Program, inst.program_id)
        if program is None or not (program.schedulable and program.is_published):
            inst.schedule_enabled = False  # program withdrawn - stop ticking
            continue
        active = db.execute(select(ProgramRun.id).where(
            ProgramRun.instance_id == inst.id,
            ProgramRun.state.in_(("queued", "running")))).first()
        if active is not None:
            inst.next_run_at = programs_svc.next_run(inst.schedule_cron, now)
            continue
        if _out_of_credits(db, inst.org_id):
            run = ProgramRun(program_id=program.id, instance_id=inst.id,
                             org_id=inst.org_id, kind="schedule", state="failed",
                             error="insufficient credits - schedule disabled",
                             started_at=now, finished_at=now)
            db.add(run)
            inst.schedule_enabled = False
            db.flush()
            hooks.append((run, program, inst))
            continue
        run = ProgramRun(program_id=program.id, instance_id=inst.id,
                         org_id=inst.org_id, kind="schedule")
        db.add(run)
        inst.next_run_at = programs_svc.next_run(inst.schedule_cron, now)
        db.flush()
        dispatch.append(run.id)


def _dispatch_deferred(db: Session, dispatch: list[str], now) -> None:
    """§28 hooks: a hook run enqueued while its instance was busy is parked
    "queued" by run_program's serialization guard. Once the instance is free,
    dispatch its OLDEST queued run (one per instance per sweep - the guard makes
    an accidental double-dispatch a no-op). A short grace keeps this from racing
    the eager dispatch of a run enqueued milliseconds ago."""
    queued = db.execute(select(ProgramRun).where(
        ProgramRun.state == "queued",
        ProgramRun.instance_id.is_not(None),
        ProgramRun.created_at <= now - timedelta(seconds=60))
        .order_by(ProgramRun.created_at)).scalars().all()
    seen: set[str] = set()
    for run in queued:
        if run.instance_id in seen:
            continue
        seen.add(run.instance_id)
        busy = db.execute(select(ProgramRun.id).where(
            ProgramRun.instance_id == run.instance_id,
            ProgramRun.state == "running").limit(1)).first()
        if busy is None:
            dispatch.append(run.id)


def _requeue_lost(db: Session, dispatch: list[str], now) -> None:
    """A run committed as "queued" whose enqueue was lost (process died between
    commit and send_task) would block its instance forever - re-dispatch after
    a grace period; run_program's queued-state guard makes duplicates no-ops."""
    lost = db.execute(select(ProgramRun.id).where(
        ProgramRun.state == "queued",
        ProgramRun.created_at <= now - timedelta(minutes=10))).scalars().all()
    dispatch.extend(lost)


def _reap_stale_runs(db: Session, hooks: list, now) -> None:
    running = db.execute(select(ProgramRun).where(
        ProgramRun.state == "running")).scalars().all()
    for run in running:
        program = db.get(Program, run.program_id)
        limit = timedelta(minutes=(program.timeout_minutes if program else 15) + 10)
        if run.started_at is not None and now - run.started_at > limit:
            run.state = "failed"
            run.error = "run lost (worker or deployer restarted mid-run)"
            run.finished_at = now
            inst = db.get(ProgramInstance, run.instance_id) if run.instance_id else None
            if program is not None and inst is not None:
                hooks.append((run, program, inst))


# ---------------------------------------------------------------- cleanup

@celery.task(name="app.workers.programs.cleanup_program_sandbox")
def cleanup_program_sandbox(program_id: str, instance_id: str | None) -> None:
    """After an instance (or a whole program) is deleted: remove the DinD
    sandbox + its layer-cache volume and the workspace artifact dir. Rows are
    already gone; everything here is best-effort."""
    name = programs_svc.sandbox_name(program_id, instance_id)
    try:
        deployer_client.cleanup_program_sandbox(name)
    except deployer_client.DeployerError as exc:
        log.warning("program sandbox cleanup %s: %s", name, exc)
    shutil.rmtree(programs_svc.instance_dir(program_id, instance_id), ignore_errors=True)
