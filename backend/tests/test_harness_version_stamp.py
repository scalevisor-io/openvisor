"""The harness fingerprint is stamped on every dev run (Phase 0: pin HARNESS_VERSION).

A build's outcome is only attributable if we record which harness config produced
it. _mark_dispatch_start (before every runner dispatch) and run_development's start
both stamp project.dev_harness_version with the deterministic agent_eval fingerprint,
and it is surfaced in the project + dev-logs serializers.
"""
import pytest

from app.api.serializers import project_out
from app.core.config import settings
from app.core.db import SyncSession
from app.models import Organization, Project
from app.services.agent_eval.harness_version import compute_harness_version
from app.workers import tasks


@pytest.fixture
def quiet(monkeypatch):
    """No redis/broker: _mark_dispatch_start only commits, but keep parity with the
    other worker tests so a stray event publish can't reach a real bus."""
    from app.services import events
    monkeypatch.setattr(events, "publish_sync", lambda *a, **k: None, raising=False)


def _project(db):
    org = Organization(name="HV Test Org", credit_balance=100.0)
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai",
                status="development", dev_run_state="running")
    db.add(p)
    db.flush()
    return p


def test_fingerprint_is_stable_and_well_formed():
    hv = compute_harness_version(settings)
    assert hv.startswith("hv_")
    assert hv == compute_harness_version(settings)  # deterministic


def test_mark_dispatch_start_stamps_the_harness_version(quiet):
    with SyncSession() as db:
        p = _project(db)
        assert p.dev_harness_version is None
        tasks._mark_dispatch_start(db, p)
        assert p.dev_harness_version == compute_harness_version(settings)
        db.rollback()


def test_project_serializer_exposes_the_harness_version(quiet):
    with SyncSession() as db:
        p = _project(db)
        p.dev_harness_version = compute_harness_version(settings)
        db.flush()
        db.refresh(p, ["repos"])
        assert project_out(p)["dev_harness_version"] == p.dev_harness_version
        db.rollback()
