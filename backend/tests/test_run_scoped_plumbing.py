"""§parallel-builds MR2: run-scoped plumbing with legacy defaults. run_ws is
THE sanctioned workspace join (legacy rows resolve exactly to
Project.workspace_path), the run travels bound to the project instance, the
feed follows it, and the wallet writers decrement atomically.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SyncSession
from app.models import CreditTransaction, DevRun, Organization, Project
from app.services import dev_concurrency, devfeed, llm
from app.workers import tasks


def _p(ws="/workspaces/p1"):
    return SimpleNamespace(id="p1", workspace_path=ws)


def test_run_ws_resolution_and_binding():
    p = _p()
    # no bound run, no row -> the legacy project workspace
    assert dev_concurrency.run_ws(p) == Path("/workspaces/p1")
    legacy = SimpleNamespace(id="r1", workspace_dir="")
    parallel = SimpleNamespace(id="r2", workspace_dir="devruns/p1/r2")
    assert dev_concurrency.run_ws(p, legacy) == Path("/workspaces/p1")
    assert dev_concurrency.run_ws(p, parallel) == (
        Path(settings.workspaces_dir) / "devruns/p1/r2")
    # the bound run drives every helper that doesn't get the row explicitly
    dev_concurrency.bind_run(p, parallel)
    assert dev_concurrency.run_ws(p) == Path(settings.workspaces_dir) / "devruns/p1/r2"
    assert devfeed.feed_path(p).as_posix().endswith("devruns/p1/r2/.openvisor/events.jsonl")
    assert dev_concurrency.runner_name(p) == "dev-p1-r2"
    dev_concurrency.bind_run(p, legacy)
    assert devfeed.feed_path(p) == Path("/workspaces/p1/.openvisor/events.jsonl")
    assert dev_concurrency.runner_name(p) == ""


def test_no_direct_workspace_joins_in_run_pipeline():
    # The leak-scan/feed fail-closed contracts are load-bearing on every run-
    # pipeline path going through run_ws: a missed re-root would redact run A's
    # feed with run B's fingerprints. The only sanctioned direct joins target
    # the CANONICAL checkout by design: provision mkdir, the demo-tail workdir,
    # the run-dir clone seed source, and the merge-time root refresh.
    src = Path(tasks.__file__).read_text()
    joins = re.findall(r"Path\(project\.workspace_path[^)]*\)", src)
    assert len(joins) == 4, joins


def test_wallet_debit_is_atomic_update(monkeypatch):
    monkeypatch.setattr(llm, "cost_credits", lambda *a, **k: 2.5)
    monkeypatch.setattr(llm, "_endpoint_price", lambda db, model: None)
    with SyncSession() as db:
        org = Organization(name="Atomic Wallet Org", credit_balance=10.0)
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai",
                    status="development")
        db.add(p)
        db.commit()
        oid, pid = org.id, p.id
    try:
        usage = {"model": "m", "input_tokens": 10, "output_tokens": 5}
        with SyncSession() as db:
            p = db.get(Project, pid)
            org = db.get(Organization, oid)  # preload: the expire must keep it honest
            llm.record_usage(db, p, dict(usage), "t1")
            llm.record_usage(db, p, dict(usage), "t2")
            # the already-loaded instance re-reads the atomically decremented value
            assert org.credit_balance == 5.0
            db.commit()
        with SyncSession() as db:
            assert db.get(Organization, oid).credit_balance == 5.0
            assert db.get(Project, pid).tokens_consumed == 30
    finally:
        with SyncSession() as db:
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.id == pid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def test_bill_dev_run_accumulates_on_the_bound_row(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "cost_credits", lambda *a, **k: 1.0)
    monkeypatch.setattr(llm, "_endpoint_price", lambda db, model: None)
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    with SyncSession() as db:
        org = Organization(name="BillRow Org", credit_balance=10.0)
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai",
                    status="development", workspace_path=str(tmp_path))
        db.add(p)
        db.commit()
        oid, pid = org.id, p.id
    try:
        with SyncSession() as db:
            p = db.get(Project, pid)
            run = dev_concurrency.acquire_slot(db, p)
            dev_concurrency.bind_run(p, run)
            ov = tmp_path / ".openvisor"
            ov.mkdir()
            (ov / "usage.json").write_text(
                '{"model": "m", "input_tokens": 100, "output_tokens": 20}')
            tasks._bill_dev_run(db, p)
            assert run.tokens_consumed == 120
            assert run.billed_through == 120
            assert run.cost_credits == 1.0
            assert not (ov / "usage.json").exists()  # read-then-unlink kept
            db.rollback()
    finally:
        with SyncSession() as db:
            db.execute(delete(DevRun).where(DevRun.project_id == pid))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.id == pid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()
