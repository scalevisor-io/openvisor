"""The project payload exposes the plan-gate status (dev_plan_status).

The shared-ui NowPanel keys its headline on it: without the field, a project parked
at the plan gate (status awaiting_customer, run idle) rendered the failed-build copy.
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



def test_project_out_exposes_the_plan_gate_status(quiet):
    """§working method plan gate: the SPA's NowPanel keys its copy on
    dev_plan_status - a proposed plan must never read as an interrupted build."""
    with SyncSession() as db:
        p = _project(db)
        assert project_out(p)["dev_plan_status"] is None
        p.dev_plan_status = "proposed"
        db.flush()
        assert project_out(p)["dev_plan_status"] == "proposed"
