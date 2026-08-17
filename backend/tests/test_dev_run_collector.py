"""Phase 0: persist one agent-eval RunRecord per dev-run outcome (collect.py).

capture_run_record derives gate signals from the project's final persisted state
(dev_run_state, dev_run_error, dev_security_review, token/credit counters) - the
error strings are pipeline constants we own, so the derivation is reliable - plus
the runner's own token snapshot for the input/output split. These tests pin the
derivation, the split (billing has already unlinked usage.json by capture time),
the attempt auto-increment (pass@1 vs pass@k), the RunRecord round-trip, and that
a captured batch feeds report.aggregate.
"""
import json
from datetime import timedelta

from app.core.config import settings
from app.core.db import SyncSession
from app.models import DevRun, DevRunRecord, Organization, Project, utcnow
from app.services.agent_eval import collect
from app.services.agent_eval.metrics import RunRecord, is_pass
from app.services.agent_eval.report import aggregate

_real_read_progress = collect.devfeed.read_progress


def _org(db, balance=100.0):
    org = Organization(name="Collector Org", credit_balance=balance)
    db.add(org)
    db.flush()
    return org


def _project(db, org, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("speciality", "general-webapp")
    kw.setdefault("dev_harness_version", "hv_test000000")
    p = Project(org_id=org.id, **kw)
    db.add(p)
    db.flush()
    return p


def _capture(db, project, *, tokens0=0, credits0=0.0, secs=42.0):
    return collect.capture_run_record(
        db, project, tokens0=tokens0, credits0=credits0,
        t_start=utcnow() - timedelta(seconds=secs))


def test_published_run_is_a_boot_pass_and_clean_gates():
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="awaiting_merge",
                     tokens_consumed=1000, cost_credits=2.5)
        rec = _capture(db, p, tokens0=200, credits0=0.5)
        db.flush()
        assert rec.boot_result is True
        assert rec.final_state == "awaiting_merge"
        assert rec.input_tokens == 800 and rec.credits == 2.0
        assert rec.attempt == 1 and rec.harness_version == "hv_test000000"
        rr = collect.to_run_record(rec)
        assert isinstance(rr, RunRecord) and is_pass(rr)  # deploying/awaiting_merge + clean gates
        db.rollback()


def test_output_tokens_come_from_the_runner_snapshot():
    """§metering split: output carries the reasoning spend, so a record that reports
    0 for it makes every harness comparison score on the input side alone."""
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="awaiting_merge", tokens_consumed=1000)
        collect.devfeed.read_progress = lambda _p: {"input_tokens": 820,
                                                    "output_tokens": 180}
        try:
            rec = _capture(db, p)
        finally:
            collect.devfeed.read_progress = _real_read_progress
        # input + output always reconciles with the billed total
        assert (rec.input_tokens, rec.output_tokens) == (820, 180)
        assert rec.input_tokens + rec.output_tokens == 1000
        db.rollback()


def test_the_split_is_read_from_the_RUNS_workspace_not_the_legacy_checkout(
        tmp_path, monkeypatch):
    """The regression the monkeypatched tests below cannot see: this capture runs
    in a fresh session where no run is bound to the project, so `run_ws` fell back
    to Project.workspace_path - where a parallel-mode run keeps no progress.json -
    and every split silently read zero, exactly like the hardcoded 0 it replaced."""
    monkeypatch.setattr(settings, "workspaces_dir", str(tmp_path))
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="awaiting_merge", tokens_consumed=1000,
                     workspace_path=str(tmp_path / "legacy-checkout"))
        run_dir = f"devruns/{p.id}/run-1"
        db.add(DevRun(project_id=p.id, state="running", workspace_dir=run_dir))
        db.flush()
        snap = tmp_path / run_dir / ".openvisor"
        snap.mkdir(parents=True)
        (snap / "progress.json").write_text(json.dumps(
            {"model": "m", "input_tokens": 780, "output_tokens": 220}))

        rec = _capture(db, p)            # the REAL read_progress, no monkeypatch
        assert (rec.input_tokens, rec.output_tokens) == (780, 220)
        db.rollback()


def test_output_tokens_are_clamped_to_the_billed_total():
    """A snapshot the run outlived (or a torn read) must never drive input negative."""
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="awaiting_merge", tokens_consumed=500)
        collect.devfeed.read_progress = lambda _p: {"output_tokens": 9_000}
        try:
            rec = _capture(db, p)
        finally:
            collect.devfeed.read_progress = _real_read_progress
        assert (rec.input_tokens, rec.output_tokens) == (0, 500)
        db.rollback()


def test_missing_snapshot_degrades_to_input_only():
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="awaiting_merge", tokens_consumed=700)
        collect.devfeed.read_progress = lambda _p: None
        try:
            rec = _capture(db, p)
        finally:
            collect.devfeed.read_progress = _real_read_progress
        assert (rec.input_tokens, rec.output_tokens) == (700, 0)
        db.rollback()


def test_boot_failure_is_recorded_as_boot_false():
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="failed",
                     dev_run_error="Demo boot check failed")
        rec = _capture(db, p)
        assert rec.boot_result is False
        assert not is_pass(collect.to_run_record(rec))
        db.rollback()


def test_leak_block_is_flagged():
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="failed",
                     dev_run_error="Blocked by pre-publish leak scan")
        rec = _capture(db, p)
        assert rec.leak_blocked is True and rec.boot_result is None
        db.rollback()


def test_security_signals_from_review_snapshot():
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="awaiting_merge",
                     dev_security_review={"verdict": "changes_requested",
                                          "findings": [{"severity": "high", "issue": "x"},
                                                       {"severity": "low", "issue": "y"}]})
        rec = _capture(db, p)
        assert rec.security_ran is True and rec.security_blocking == 1  # only the high counts
        db.rollback()


def test_review_unavailable_counts_as_not_ran():
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="awaiting_merge",
                     dev_security_review={"verdict": "review_unavailable"})
        rec = _capture(db, p)
        assert rec.security_ran is False
        db.rollback()


def test_attempt_auto_increments_across_resumes():
    with SyncSession() as db:
        p = _project(db, _org(db), dev_run_state="failed", dev_run_error="Runner exited 1")
        r1 = _capture(db, p); db.flush()
        r2 = _capture(db, p); db.flush()
        assert (r1.attempt, r2.attempt) == (1, 2)
        db.rollback()


def test_load_records_scopes_by_version_and_feeds_aggregate():
    with SyncSession() as db:
        org = _org(db)
        a = _project(db, org, dev_run_state="awaiting_merge", tokens_consumed=500,
                     cost_credits=1.0, dev_harness_version="hv_aaaaaaaaaaaa")
        _capture(db, a); db.flush()
        b = _project(db, org, dev_run_state="failed", dev_run_error="Demo boot check failed",
                     dev_harness_version="hv_bbbbbbbbbbbb")
        _capture(db, b); db.flush()

        one = collect.load_records(db, source="live", harness_version="hv_aaaaaaaaaaaa")
        assert len(one) == 1 and one[0].harness_version == "hv_aaaaaaaaaaaa"
        rep = aggregate(collect.load_records(db, source="live"))
        assert rep.n_attempts >= 2 and rep.n_specs >= 2
        # two distinct versions present -> the report must warn, never compare across them
        assert any("harness version" in w for w in rep.warnings)
        db.rollback()
