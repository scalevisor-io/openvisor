"""Programs (§28) DB-backed tests (SyncSession + rollback, test_knowledge.py
style): run billing at the per-program markup, the schedule sweep, the stale
reaper, and the whole-KB fingerprint source for the output scan."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import SyncSession
from app.models import (
    CreditTransaction, Organization, Program, ProgramInstance,
    ProgramRun, utcnow,
)
from app.services import leakscan
from app.services.pricing import cost_credits
from app.workers import programs as wp

_N = iter(range(10000))


def _program(db, **kw):
    kw.setdefault("title", "Prog")
    kw.setdefault("gitlab_repo_path", f"grp/prog-{next(_N)}")
    kw.setdefault("is_published", True)
    kw.setdefault("schedulable", True)
    p = Program(**kw)
    db.add(p)
    db.flush()
    return p


def _org(db, balance=100.0):
    org = Organization(name="Programs Test Org", credit_balance=balance)
    db.add(org)
    db.flush()
    return org


def _instance(db, program, org, **kw):
    kw.setdefault("schedule_enabled", True)
    kw.setdefault("schedule_cron", "*/15 * * * *")
    kw.setdefault("next_run_at", utcnow() - timedelta(minutes=1))
    inst = ProgramInstance(program_id=program.id, org_id=org.id,
                           ssh_public_key="pk", ssh_private_key_enc="enc", **kw)
    db.add(inst)
    db.flush()
    return inst


# ---- billing ----

def test_bill_program_run_debits_wallet_at_program_markup(tmp_path):
    usage = {"model": "mistral-large-latest", "input_tokens": 1000, "output_tokens": 500}
    (tmp_path / "usage.json").write_text(
        '{"model": "mistral-large-latest", "input_tokens": 1000, "output_tokens": 500}')
    expected = cost_credits(usage["model"], 1000, 500, markup=2.0)

    with SyncSession() as db:
        org = _org(db)
        program = _program(db, credit_markup=2.0)
        run = ProgramRun(program_id=program.id, org_id=org.id, kind="manual")
        db.add(run)
        db.flush()

        wp._bill(db, run, program, tmp_path)

        assert run.tokens_input == 1000 and run.tokens_output == 500
        assert run.cost_credits == pytest.approx(expected)
        assert org.credit_balance == pytest.approx(100.0 - expected)
        txns = db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == org.id)).scalars().all()
        assert len(txns) == 1
        assert txns[0].kind == "program_run"
        assert txns[0].project_id is None
        assert txns[0].amount == pytest.approx(-expected)
        assert not (tmp_path / "usage.json").exists()  # never billed twice
        db.rollback()


def test_bill_program_run_prices_cached_reads_at_cached_rate(tmp_path):
    # mistral-large-latest carries cached_input 0.05 vs input 0.50: 800k of the
    # 1M input tokens were prompt-cache reads, so the bill drops accordingly
    # and the ledger row records the cached subset.
    (tmp_path / "usage.json").write_text(
        '{"model": "mistral-large-latest", "input_tokens": 1000000, '
        '"output_tokens": 0, "cached_input_tokens": 800000}')
    expected = cost_credits("mistral-large-latest", 1_000_000, 0, markup=2.0,
                            cached_input_tokens=800_000)
    flat = cost_credits("mistral-large-latest", 1_000_000, 0, markup=2.0)

    with SyncSession() as db:
        org = _org(db)
        program = _program(db, credit_markup=2.0)
        run = ProgramRun(program_id=program.id, org_id=org.id, kind="manual")
        db.add(run)
        db.flush()

        wp._bill(db, run, program, tmp_path)

        assert expected < flat  # the discount actually applied
        assert run.cost_credits == pytest.approx(expected)
        txn = db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == org.id)).scalars().one()
        assert txn.amount == pytest.approx(-expected)
        assert txn.tokens_cached == 800_000
        db.rollback()


def test_bill_check_run_discards_report_without_billing(tmp_path):
    (tmp_path / "usage.json").write_text(
        '{"model": "mistral-large-latest", "input_tokens": 10, "output_tokens": 10}')
    with SyncSession() as db:
        program = _program(db)
        run = ProgramRun(program_id=program.id, kind="check")  # org_id NULL
        db.add(run)
        db.flush()
        wp._bill(db, run, program, tmp_path)
        assert run.cost_credits == 0.0
        assert not (tmp_path / "usage.json").exists()
        db.rollback()


# ---- schedule sweep ----

def test_sweep_dispatches_due_instance_and_recomputes_next(monkeypatch):
    with SyncSession() as db:
        org = _org(db)
        program = _program(db)
        inst = _instance(db, program, org)
        now = utcnow()
        dispatch, hooks = [], []
        wp._sweep_due(db, now, dispatch, hooks)

        runs = db.execute(select(ProgramRun).where(
            ProgramRun.instance_id == inst.id)).scalars().all()
        assert len(runs) == 1
        assert runs[0].kind == "schedule" and runs[0].state == "queued"
        assert dispatch == [runs[0].id]
        assert hooks == []
        assert inst.next_run_at > now
        db.rollback()


def test_sweep_skips_instance_with_active_run():
    with SyncSession() as db:
        org = _org(db)
        program = _program(db)
        inst = _instance(db, program, org)
        db.add(ProgramRun(program_id=program.id, instance_id=inst.id,
                          org_id=org.id, state="running", started_at=utcnow()))
        db.flush()
        now = utcnow()
        dispatch, hooks = [], []
        wp._sweep_due(db, now, dispatch, hooks)
        assert dispatch == [] and hooks == []
        states = db.execute(select(ProgramRun.state).where(
            ProgramRun.instance_id == inst.id)).scalars().all()
        assert states == ["running"]  # no second run queued
        assert inst.next_run_at > now  # but the tick was rescheduled
        db.rollback()


def test_sweep_insufficient_credits_fails_visibly_and_disables():
    with SyncSession() as db:
        org = _org(db, balance=0.0)
        program = _program(db)
        inst = _instance(db, program, org)
        dispatch, hooks = [], []
        wp._sweep_due(db, utcnow(), dispatch, hooks)
        assert dispatch == []
        assert len(hooks) == 1
        run = hooks[0][0]
        assert run.state == "failed" and "insufficient credits" in run.error
        assert inst.schedule_enabled is False
        db.rollback()


def test_sweep_unpublished_program_stops_the_schedule():
    with SyncSession() as db:
        org = _org(db)
        program = _program(db, is_published=False)
        inst = _instance(db, program, org)
        dispatch, hooks = [], []
        wp._sweep_due(db, utcnow(), dispatch, hooks)
        assert dispatch == [] and hooks == []
        assert inst.schedule_enabled is False
        db.rollback()


def test_reap_stale_running_run(monkeypatch):
    with SyncSession() as db:
        org = _org(db)
        program = _program(db, timeout_minutes=15)
        inst = _instance(db, program, org, schedule_enabled=False, next_run_at=None)
        stale = ProgramRun(program_id=program.id, instance_id=inst.id, org_id=org.id,
                           state="running", started_at=utcnow() - timedelta(minutes=40))
        fresh = ProgramRun(program_id=program.id, instance_id=inst.id, org_id=org.id,
                           state="running", started_at=utcnow() - timedelta(minutes=2))
        db.add_all([stale, fresh])
        db.flush()
        hooks = []
        wp._reap_stale_runs(db, hooks, utcnow())
        assert stale.state == "failed" and "run lost" in stale.error
        assert fresh.state == "running"
        assert [r.id for r, _p, _i in hooks] == [stale.id]
        db.rollback()


def test_requeue_lost_queued_runs():
    with SyncSession() as db:
        org = _org(db)
        program = _program(db)
        lost = ProgramRun(program_id=program.id, org_id=org.id, kind="manual",
                          created_at=utcnow() - timedelta(minutes=11))
        fresh = ProgramRun(program_id=program.id, org_id=org.id, kind="manual")
        db.add_all([lost, fresh])
        db.flush()
        dispatch = []
        wp._requeue_lost(db, dispatch, utcnow())
        assert dispatch == [lost.id]
        db.rollback()


# ---- KB fingerprints from the ingested knowledge base (Meilisearch 'kb' index) ----

def test_kb_fingerprints_from_db_covers_kb_only(monkeypatch):
    kb_text = ("sovereign infrastructure isolation guidance " * 12).strip()
    # all_kb_docs yields only KB documents (CVEs never enter the Meili index), so the
    # fingerprints cover the KB and only the KB.
    monkeypatch.setattr(leakscan.meili, "all_kb_docs", lambda: iter([kb_text]))
    fps = leakscan.kb_fingerprints_from_db(cap=1_000_000)
    assert fps
    norm = leakscan.norm_ws(kb_text)
    assert any(fp in norm for fp in fps)
    assert not any("cve advisory" in fp for fp in fps)  # cve corpus is not in the KB index
