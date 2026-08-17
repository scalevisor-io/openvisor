"""Bridge the live dev pipeline to agent_eval: persist one RunRecord-shaped row
per dev-run outcome, and load them back for report.aggregate.

The capture derives its gate signals from state the pipeline ALREADY persists on
the project at run end (dev_run_state, dev_run_error, dev_security_review, the
token/credit counters) plus the runner's own per-run token snapshot for the
input/output split - no new plumbing threaded through the build loops. The
error strings are pipeline constants we own, so the derivation is reliable, not
guesswork. It is best-effort by contract: capturing an eval record must NEVER
break a build, so run_development calls capture_run_record inside a guarded
finally.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents import pipeline
from app.core.config import settings
from app.models import DevRun, DevRunRecord, Project, utcnow
from app.services import dev_concurrency, devfeed
from app.services.agent_eval.metrics import RunRecord, _PASS_STATES

# Pipeline-owned error strings that pin a specific gate outcome (see workers/tasks.py).
_BOOT_FAIL_ERROR = "Demo boot check failed"
_LEAK_BLOCK_ERROR = "Blocked by pre-publish leak scan"


def _run_model(db: Session, project: Project) -> str:
    """The model that ran, normalised like the runner's usage.json
    (LLM_MODEL.split('/')[-1]). Best-effort - never raises into the capture."""
    try:
        from app.workers.tasks import _project_model_config
        model = _project_model_config(db, project)[2] or ""
        return model.split("/")[-1]
    except Exception:
        return ""


def _security_signals(review: dict | None) -> tuple[bool, int]:
    """(ran, blocking_count) from a project's dev_security_review snapshot.

    security_ran is False ONLY for the fail-closed "review_unavailable" park (the
    review was on this run's path but could not run). An ABSENT review means the
    §14.7 review was not this path's gate at all (platform auto-merge gates on CI,
    scaffold has no publish step) - that is not a failed security gate, so it
    counts as ran-clean with zero blocking findings."""
    if not review:
        return True, 0
    if review.get("verdict") == "review_unavailable":
        return False, 0
    # Count what actually GATES (pipeline.blocking_findings), not raw severity - a high
    # correctness finding is advisory and must not inflate the eval's blocking count.
    blocking = len(pipeline.blocking_findings(review.get("findings", [])))
    return True, blocking


def capture_run_record(db: Session, project: Project, *, tokens0: int,
                       credits0: float, t_start: datetime) -> DevRunRecord:
    """Write one DevRunRecord for the run that just finished, derived from the
    project's final persisted state. Caller commits. attempt auto-increments over
    prior live records for the same project (pass@1 vs pass@k)."""
    state = project.dev_run_state or ""
    err = project.dev_run_error or ""

    boot_result: bool | None = None
    if err == _BOOT_FAIL_ERROR:
        boot_result = False
    elif state in _PASS_STATES and settings.dev_boot_check:
        # It published/deployed with the gate ON, so the demo booted (or the gate
        # was unavailable and failed open - the rarer case, refined when contract
        # capture lands). Treat as a boot pass.
        boot_result = True

    ran, blocking = _security_signals(project.dev_security_review)
    acc = project.dev_acceptance or {}
    acceptance_passed = acc.get("passed")
    acceptance_total = acc.get("total")
    tokens = max(0, (project.tokens_consumed or 0) - (tokens0 or 0))
    credits = max(0.0, round((project.cost_credits or 0.0) - (credits0 or 0.0), 6))
    # §metering split: output tokens carry the reasoning spend, which on a
    # reasoning model is the expensive half - recording 0 for them made every
    # harness comparison score on the input side alone. Billing already consumed
    # (and unlinked) usage.json by the time we run, so the runner's surviving
    # per-run snapshot is the only source for the split. Output is clamped to the
    # billed total so input + output always reconciles with `credits`; an absent
    # or torn snapshot degrades to input-only rather than losing the row.
    # The snapshot lives in the RUN's workspace, which `run_ws` finds through the
    # run bound to the project INSTANCE - a session-local attribute. This capture
    # runs inside `run_development`'s finally, which re-loads the project in a
    # FRESH session, so nothing is bound there and the join silently falls back to
    # the legacy checkout, where a parallel-mode run keeps no progress.json. Bind
    # the run explicitly (in-memory only) or the split reads as zero exactly like
    # the hardcoded value it replaced - which is how it shipped the first time.
    output_tokens = 0
    try:
        run = db.execute(
            select(DevRun).where(DevRun.project_id == project.id)
            .order_by(DevRun.created_at.desc())).scalars().first()
        if run is not None:
            dev_concurrency.bind_run(project, run)
        snapshot = devfeed.read_progress(project) or {}
        output_tokens = min(max(0, int(snapshot.get("output_tokens") or 0)), tokens)
    except Exception:  # noqa: BLE001 - metering detail must never break the capture
        output_tokens = 0
    prior = db.execute(
        select(func.count()).select_from(DevRunRecord)
        .where(DevRunRecord.spec_id == project.id, DevRunRecord.source == "live")
    ).scalar_one()

    rec = DevRunRecord(
        project_id=project.id, spec_id=project.id, speciality=project.speciality,
        harness_version=project.dev_harness_version, model=_run_model(db, project),
        attempt=int(prior) + 1, final_state=state[:16], boot_result=boot_result,
        contract_ok=None, ci_status=None, security_blocking=blocking, security_ran=ran,
        leak_blocked=(err == _LEAK_BLOCK_ERROR), leak_scanner_errored=False,
        input_tokens=tokens - output_tokens, output_tokens=output_tokens, credits=credits,
        wall_clock_s=round((utcnow() - t_start).total_seconds(), 1),
        acceptance_passed=acceptance_passed, acceptance_total=acceptance_total,
        error=(err or None) and err[:512], source="live",
    )
    db.add(rec)
    return rec


def to_run_record(row: DevRunRecord) -> RunRecord:
    """DevRunRecord row -> the pure RunRecord report.aggregate consumes."""
    return RunRecord(
        spec_id=row.spec_id, speciality=row.speciality or "",
        harness_version=row.harness_version or "", model=row.model or "",
        attempt=row.attempt, final_state=row.final_state, boot_result=row.boot_result,
        contract_ok=row.contract_ok, ci_status=row.ci_status,
        security_blocking=row.security_blocking, security_ran=row.security_ran,
        leak_blocked=row.leak_blocked, leak_scanner_errored=row.leak_scanner_errored,
        input_tokens=row.input_tokens, output_tokens=row.output_tokens,
        credits=row.credits, wall_clock_s=row.wall_clock_s,
        acceptance_passed=row.acceptance_passed, acceptance_total=row.acceptance_total,
        error=row.error or "",
    )


def load_records(db: Session, *, source: str | None = "live",
                 harness_version: str | None = None) -> list[RunRecord]:
    """Load persisted eval records as RunRecords (optionally scoped to one source
    and/or one harness version - the report must never mix harness versions)."""
    q = select(DevRunRecord)
    if source is not None:
        q = q.where(DevRunRecord.source == source)
    if harness_version is not None:
        q = q.where(DevRunRecord.harness_version == harness_version)
    return [to_run_record(r) for r in db.execute(q).scalars().all()]
