"""§request help: the free escalation over a PLATFORM fault.

Three seams, and the point of the feature lives in all three: the park stamps
who was at fault, the capability turns that stamp into an affordance, and the
endpoint refuses to give consulting away over an ordinary build failure.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.serializers import dev_help_capability
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, StatusChange, User,
)
from app.services import dev_faults
from app.workers import tasks


# ---- the vocabulary ----

def test_runner_categories_map_to_platform_and_nothing_else():
    for category in ("agent_error", "llm_auth", "llm_model", "llm_unreachable"):
        assert dev_faults.from_runner_category(category) == dev_faults.PLATFORM
    # An unknown category must not start handing out free consultant time.
    for category in (None, "", "task_impossible", "some_future_category"):
        assert dev_faults.from_runner_category(category) is None


# ---- the park ----

class _P:
    """The bare shape _save_run/_runner_exit_copy touch."""
    id = "p1"
    kind = "ai"
    dev_run_state = "running"
    dev_run_error = None
    dev_run_fault = None
    dev_run_log = None
    workspace_path = None
    dev_request_id = None


def _quiet(monkeypatch):
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(tasks.events, "publish_sync", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "object_session", lambda p: None)


def test_save_run_stamps_and_then_clears_the_fault(monkeypatch):
    _quiet(monkeypatch)
    p = _P()
    tasks._save_run(p, "failed", error="boom", fault=dev_faults.PLATFORM)
    assert p.dev_run_fault == dev_faults.PLATFORM
    # The next state a run reaches owns the verdict - a stale "our fault" must
    # never survive onto a run that went fine.
    tasks._save_run(p, "running")
    assert p.dev_run_fault is None
    tasks._save_run(p, "failed", error="the run produced no changes to publish")
    assert p.dev_run_fault is None


def test_runner_exit_copy_blames_the_platform_for_a_driver_crash(monkeypatch, tmp_path):
    _quiet(monkeypatch)
    ws = tmp_path / "ws"
    (ws / ".openvisor").mkdir(parents=True)
    (ws / ".openvisor" / "error.json").write_text(json.dumps({
        "category": "agent_error",
        "message": "the build agent crashed (ResultError: unrecognized_model)"}))
    monkeypatch.setattr(tasks.dev_concurrency, "run_ws", lambda project: ws)
    chat, detail, fault = tasks._runner_exit_copy(_P(), {"exit_code": "1"})
    assert fault == dev_faults.PLATFORM
    assert "unrecognized_model" in detail and "crashed" in chat


def test_runner_exit_copy_leaves_a_refused_deploy_key_to_the_customer(monkeypatch, tmp_path):
    _quiet(monkeypatch)
    monkeypatch.setattr(tasks.dev_concurrency, "run_ws", lambda project: tmp_path)
    monkeypatch.setattr(tasks, "_push_failure_hint", lambda logs: None)
    _chat, _detail, fault = tasks._runner_exit_copy(
        _P(), {"exit_code": "6", "logs": "GIT_REMOTE_DENIED"})
    assert fault is None, "a repo that refused our key is the customer's to fix"
    # ...while a remote the sandbox could never REACH is infrastructure, ours.
    _chat, _detail, fault = tasks._runner_exit_copy(
        _P(), {"exit_code": "6", "logs": "GIT_REMOTE_UNREACHABLE"})
    assert fault == dev_faults.PLATFORM


def test_a_driver_that_died_without_a_report_is_still_ours(monkeypatch, tmp_path):
    _quiet(monkeypatch)
    monkeypatch.setattr(tasks.dev_concurrency, "run_ws", lambda project: tmp_path)
    _chat, detail, fault = tasks._runner_exit_copy(_P(), {"exit_code": "1"})
    assert detail == "" and fault == dev_faults.PLATFORM


# ---- the capability ----

def _proj(**kw):
    p = Project(org_id="o", name="P", description="d", kind="ai",
                status="development", dev_run_state="failed",
                dev_run_fault=dev_faults.PLATFORM)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_capability_offers_help_only_over_a_platform_fault():
    ok, blocker = dev_help_capability(_proj())
    assert ok and blocker is None

    ok, blocker = dev_help_capability(_proj(dev_run_fault=None))
    assert not ok and "its own terms" in blocker

    # Already escalated: the button is the filing, not a second one.
    ok, blocker = dev_help_capability(_proj(status="awaiting_admin"))
    assert not ok and "already has this project" in blocker

    for status in ("canceled", "finished"):
        ok, _ = dev_help_capability(_proj(status=status))
        assert not ok, status
    ok, _ = dev_help_capability(_proj(kind="direct_quote"))
    assert not ok


# ---- the endpoint ----

@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="Help Org", credit_balance=50.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    events.get_sync_redis().delete("rl:login:testclient")
    with TestClient(app) as c:
        yield c
    events.get_sync_redis().delete("rl:login:testclient")


def _customer(org_id, **project_kw):
    email = f"help-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "customer-secret-123"
    with SyncSession() as db:
        db.add(User(org_id=org_id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        p = Project(org_id=org_id, name="P", description="d", kind="ai",
                    status="development", dev_run_state="failed",
                    dev_run_error="the build agent crashed", gitlab_project_id=7,
                    **project_kw)
        db.add(p)
        db.commit()
        return email, pwd, p.id


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    client.headers.update({"X-CSRF-Token": tok})


def test_request_help_escalates_free(client, org):
    email, pwd, pid = _customer(org, dev_run_fault=dev_faults.PLATFORM)
    _auth(client, email, pwd)
    body = client.get(f"/api/projects/{pid}").json()
    assert body["dev_run_fault"] == "platform" and body["dev_can_request_help"] is True

    r = client.post(f"/api/projects/{pid}/request-help")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_admin"
    # The whole point: no credits move, and no transaction is written at all.
    with SyncSession() as db:
        assert db.get(Organization, org).credit_balance == 50.0
        assert db.execute(select(CreditTransaction)
                          .where(CreditTransaction.project_id == pid)).first() is None
        bodies = db.execute(select(Message.body)
                            .where(Message.project_id == pid)).scalars().all()
    assert any("on us" in b for b in bodies)
    # Filed once: the affordance is gone while the consultant holds it.
    assert client.get(f"/api/projects/{pid}").json()["dev_can_request_help"] is False
    assert client.post(f"/api/projects/{pid}/request-help").status_code == 409


def test_request_help_refused_for_an_ordinary_build_failure(client, org):
    email, pwd, pid = _customer(org)  # failed, but no platform fault
    _auth(client, email, pwd)
    assert client.get(f"/api/projects/{pid}").json()["dev_can_request_help"] is False
    r = client.post(f"/api/projects/{pid}/request-help")
    assert r.status_code == 409 and "its own terms" in r.json()["detail"]
    with SyncSession() as db:
        assert db.get(Project, pid).status == "development"
