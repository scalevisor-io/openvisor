"""§routines: saved prompts on a project, optionally scheduled.

A routine is a template - firing one creates an ordinary Request and hands it to
the normal dispatch path. What is worth pinning is therefore NOT the pipeline
(that is the request pipeline, already covered) but the decision to fire:

- the skip-while-open guard, which is the only thing standing between a weekly
  prompt and a stack of builds on one unmerged PR (auto_dev gets this free from
  its issue-URL dedup; a routine has no such key by design),
- the cron floor, because a routine starts real dev runs,
- and the instance kill switch, which has to hold on the server whatever the SPA
  decides to render.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import (
    AppSetting, Organization, Project, ProjectRoutine, Request, User,
)
from app.services import events, routines as routines_svc


def _project(db, **kw):
    org = Organization(name="Routine Org", credit_balance=50.0)
    db.add(org)
    db.flush()
    kw.setdefault("status", "development")
    p = Project(org_id=org.id, name="P", description="d", kind="ai", **kw)
    db.add(p)
    db.flush()
    return p


def _routine(db, project, **kw):
    kw.setdefault("title", "Weekly dependency audit")
    kw.setdefault("prompt", "Audit dependencies and open a PR with the upgrades.")
    r = ProjectRoutine(project_id=project.id, **kw)
    db.add(r)
    db.flush()
    return r


# ---------------------------------------------------------------- firing

def test_firing_creates_an_ordinary_request_seeded_with_the_prompt(monkeypatch):
    """The whole design: a routine spawns the same object a customer types."""
    with SyncSession() as db:
        try:
            monkeypatch.setattr(routines_svc.dev_concurrency, "slots_full",
                                lambda db, p: False)
            project = _project(db)
            routine = _routine(db, project)
            req = routines_svc.fire(db, routine)
            assert (req.project_id, req.type, req.handling, req.status) == (
                project.id, "feature", "ai", "open")
            assert req.title == "Weekly dependency audit"
            assert routine.last_request_id == req.id
            assert routine.last_run_at is not None
        finally:
            db.rollback()


def test_a_routine_does_not_stack_on_its_own_open_request(monkeypatch):
    """The guard that replaces auto_dev's issue dedup: while last week's request
    is open, this week's tick must not add a second build."""
    with SyncSession() as db:
        try:
            monkeypatch.setattr(routines_svc.dev_concurrency, "slots_full",
                                lambda db, p: False)
            project = _project(db)
            routine = _routine(db, project)
            first = routines_svc.fire(db, routine)
            with pytest.raises(routines_svc.RoutineError, match="still open"):
                routines_svc.fire(db, routine)
            # ...and once it closes, the routine fires again
            first.status = "done"
            db.flush()
            second = routines_svc.fire(db, routine)
            assert second.id != first.id
        finally:
            db.rollback()


@pytest.mark.parametrize("state,fires", [("open", False), ("in_progress", False),
                                         ("done", True), ("rejected", True)])
def test_which_previous_states_block_a_firing(monkeypatch, state, fires):
    with SyncSession() as db:
        try:
            monkeypatch.setattr(routines_svc.dev_concurrency, "slots_full",
                                lambda db, p: False)
            project = _project(db)
            routine = _routine(db, project)
            req = routines_svc.fire(db, routine)
            req.status = state
            db.flush()
            if fires:
                assert routines_svc.fire(db, routine) is not None
            else:
                with pytest.raises(routines_svc.RoutineError):
                    routines_svc.fire(db, routine)
        finally:
            db.rollback()


def test_guards_refuse_with_copy_the_customer_can_act_on(monkeypatch):
    with SyncSession() as db:
        try:
            project = _project(db)
            routine = _routine(db, project)

            monkeypatch.setattr(routines_svc.dev_concurrency, "slots_full",
                                lambda db, p: True)
            with pytest.raises(routines_svc.RoutineError, match="already running"):
                routines_svc.fire(db, routine)

            monkeypatch.setattr(routines_svc.dev_concurrency, "slots_full",
                                lambda db, p: False)
            org = db.get(Organization, project.org_id)
            org.credit_balance = 0.0
            db.flush()
            with pytest.raises(routines_svc.RoutineError, match="credits"):
                routines_svc.fire(db, routine)
            org.credit_balance = 50.0

            routine.enabled = False
            with pytest.raises(routines_svc.RoutineError, match="paused"):
                routines_svc.fire(db, routine)
            routine.enabled = True

            project.status = "finished"
            with pytest.raises(routines_svc.RoutineError, match="finished"):
                routines_svc.fire(db, routine)
            project.status = "development"

            project.block_auto_development = True
            with pytest.raises(routines_svc.RoutineError, match="blocked"):
                routines_svc.fire(db, routine)
        finally:
            db.rollback()


def test_a_skip_is_recorded_and_the_schedule_moves_on():
    """A blocked routine must neither spin nor go silent."""
    with SyncSession() as db:
        try:
            project = _project(db)
            routine = _routine(db, project, schedule_cron="0 7 * * 1")
            routines_svc.record_skip(routine, "Previous run is still open")
            assert routine.last_skip_reason == "Previous run is still open"
            assert routine.next_run_at is not None
        finally:
            db.rollback()


# ---------------------------------------------------------------- schedule

def test_the_cron_floor_rejects_expensive_cadences():
    assert routines_svc.validate_cron("* * * * *") is not None
    assert routines_svc.validate_cron("*/5 * * * *") is not None
    assert routines_svc.validate_cron("0 7 * * 1") is None      # weekly
    assert routines_svc.validate_cron("0 * * * *") is None      # hourly == the floor
    assert routines_svc.validate_cron("nonsense") is not None
    assert routines_svc.validate_cron("") is not None


def test_the_floor_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "routine_min_schedule_minutes", 1440)
    assert routines_svc.validate_cron("0 * * * *") is not None  # hourly now refused


# ---------------------------------------------------------------- kill switch

def test_the_instance_switch_stops_firing_even_for_an_enabled_routine(monkeypatch):
    with SyncSession() as db:
        try:
            monkeypatch.setattr(routines_svc.dev_concurrency, "slots_full",
                                lambda db, p: False)
            project = _project(db)
            routine = _routine(db, project)
            db.add(AppSetting(key=routines_svc.ROUTINES_DISABLED, value=True))
            db.flush()
            assert routines_svc.enabled_sync(db) is False
            with pytest.raises(routines_svc.RoutineError, match="disabled"):
                routines_svc.fire(db, routine)
        finally:
            db.rollback()


def test_routines_are_enabled_when_no_row_was_ever_written():
    """Default-on: the flag is stored as 'disabled' so a fresh instance works."""
    with SyncSession() as db:
        try:
            db.query(AppSetting).filter_by(key=routines_svc.ROUTINES_DISABLED).delete()
            db.flush()
            assert routines_svc.enabled_sync(db) is True
        finally:
            db.rollback()


# ---------------------------------------------------------------- HTTP

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


@pytest.fixture
def customer(client):
    email = f"rt-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "routine-secret-1234"
    with SyncSession() as db:
        org = Organization(name="Routine HTTP Org", credit_balance=50.0)
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        project = Project(org_id=org.id, name="P", description="d", kind="ai",
                          status="development")
        db.add(project)
        db.commit()
        oid, pid = org.id, project.id
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    try:
        yield {"X-CSRF-Token": tok}, pid
    finally:
        with SyncSession() as db:
            db.query(ProjectRoutine).filter_by(project_id=pid).delete()
            db.query(Request).filter_by(project_id=pid).delete()
            db.query(Project).filter_by(id=pid).delete()
            db.query(User).filter_by(org_id=oid).delete()
            db.query(Organization).filter_by(id=oid).delete()
            db.commit()


def test_routine_crud_round_trip(client, customer):
    headers, pid = customer
    r = client.post(f"/api/projects/{pid}/routines", headers=headers, json={
        "title": "Weekly audit", "prompt": "Audit the dependencies.",
        "schedule_cron": "0 7 * * 1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schedule_cron"] == "0 7 * * 1"
    assert body["next_run_at"] is not None
    rid = body["id"]

    assert len(client.get(f"/api/projects/{pid}/routines", headers=headers).json()) == 1

    # clearing the cron makes it hand-fired: no next occurrence
    r = client.put(f"/api/projects/{pid}/routines/{rid}", headers=headers,
                   json={"schedule_cron": ""})
    assert r.status_code == 200 and r.json()["next_run_at"] is None

    # disabling a scheduled routine also drops its next occurrence
    client.put(f"/api/projects/{pid}/routines/{rid}", headers=headers,
               json={"schedule_cron": "0 7 * * 1"})
    r = client.put(f"/api/projects/{pid}/routines/{rid}", headers=headers,
                   json={"enabled": False})
    assert r.json()["next_run_at"] is None

    assert client.delete(f"/api/projects/{pid}/routines/{rid}",
                         headers=headers).status_code == 200
    assert client.get(f"/api/projects/{pid}/routines", headers=headers).json() == []


def test_the_api_rejects_a_cadence_under_the_floor(client, customer):
    headers, pid = customer
    r = client.post(f"/api/projects/{pid}/routines", headers=headers, json={
        "title": "Too often", "prompt": "go", "schedule_cron": "* * * * *"})
    assert r.status_code == 400 and "floor" in r.json()["detail"]


def test_writes_are_refused_while_the_instance_switch_is_off(client, customer):
    """The SPA hides the tab, but the gate is here."""
    headers, pid = customer
    with SyncSession() as db:
        db.merge(AppSetting(key=routines_svc.ROUTINES_DISABLED, value=True))
        db.commit()
    try:
        r = client.post(f"/api/projects/{pid}/routines", headers=headers,
                        json={"title": "T", "prompt": "p"})
        assert r.status_code == 403 and "disabled" in r.json()["detail"]
        assert client.get("/api/settings").json()["routines_enabled"] is False
    finally:
        with SyncSession() as db:
            db.query(AppSetting).filter_by(key=routines_svc.ROUTINES_DISABLED).delete()
            db.commit()
    assert client.get("/api/settings").json()["routines_enabled"] is True
