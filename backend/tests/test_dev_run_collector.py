"""Phase 0: persist one agent-eval RunRecord per dev-run outcome (collect.py).

capture_run_record derives gate signals from the project's final persisted state
(dev_run_state, dev_run_error, dev_security_review, token/credit counters) - the
error strings are pipeline constants we own, so the derivation is reliable. These
tests pin the derivation, the attempt auto-increment (pass@1 vs pass@k), the
RunRecord round-trip, and that a captured batch feeds report.aggregate.
"""
from datetime import timedelta

from app.core.db import SyncSession
from app.models import DevRunRecord, Organization, Project, utcnow
from app.services.agent_eval import collect
from app.services.agent_eval.metrics import RunRecord, is_pass
from app.services.agent_eval.report import aggregate


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
