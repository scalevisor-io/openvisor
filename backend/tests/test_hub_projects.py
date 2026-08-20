"""§pass-through P1: the from-hub project surface. A hub token creates projects
in brokered orgs only (source='hub', opaque subdomain, no GitLab user), can
never see direct-customer projects (hard boundary), drives actions through the
SAME project_actions guards as the SPA, and every status/message/evaluation/demo
event on a hub project lands in the transactional outbox, drained claim-based
by the Beat push."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import new_api_token
from app.main import app
from app.models import (
    ApiToken, CreditTransaction, HubCreditGrant, HubProjectEvent, Message,
    Organization, Project, ProjectMemory, ProjectRepo, Quote, Request, StatusChange,
    User,
)


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine as _async_engine
    asyncio.run(_async_engine.dispose(close=False))
    try:
        with TestClient(app) as c:
            yield c
    finally:
        asyncio.run(_async_engine.dispose(close=False))


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env():
    """Hub token + a brokered (hub_managed) org + a DIRECT org with a customer
    project, so the boundary tests have something they must NOT see."""
    with SyncSession() as db:
        admin_org = Organization(name="HubProj Admin Org")
        db.add(admin_org)
        db.flush()
        admin = User(org_id=admin_org.id, email=f"hp-{uuid.uuid4().hex}@example.com",
                     password_hash="x", role="admin", email_verified=True)
        db.add(admin)
        db.flush()
        plaintext, token_hash = new_api_token()
        db.add(ApiToken(user_id=admin.id, token_hash=token_hash, name="hub", scope="hub"))
        brokered = Organization(name="Hub customer 1f3a", hub_managed=True,
                                hub_create_key=f"k-{uuid.uuid4().hex}")
        direct = Organization(name="Direct Customer Co")
        db.add_all([brokered, direct])
        db.flush()
        direct_project = Project(org_id=direct.id, name="Direct P", description="d",
                                 kind="ai", status="development")
        db.add(direct_project)
        db.commit()
        env = {"token": plaintext, "admin_org": admin_org.id, "admin": admin.id,
               "brokered_org": brokered.id, "direct_org": direct.id,
               "direct_project": direct_project.id}
    try:
        yield env
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id.in_(
                [env["brokered_org"], env["direct_org"]]))).scalars().all()
            if pids:
                db.execute(delete(HubProjectEvent).where(HubProjectEvent.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
                db.execute(delete(ProjectMemory).where(ProjectMemory.project_id.in_(pids)))
                db.execute(delete(CreditTransaction).where(CreditTransaction.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
            db.execute(delete(Project).where(Project.id.in_(pids)))
            for oid in (env["brokered_org"], env["direct_org"]):
                db.execute(delete(HubCreditGrant).where(HubCreditGrant.org_id == oid))
                db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
                db.execute(delete(Organization).where(Organization.id == oid))
            db.execute(delete(ApiToken).where(ApiToken.user_id == env["admin"]))
            db.execute(delete(User).where(User.id == env["admin"]))
            db.execute(delete(Organization).where(Organization.id == env["admin_org"]))
            db.commit()


def _create(client, env, monkeypatch=None, **over):
    if monkeypatch is not None:
        from app.api import hub as hub_api
        monkeypatch.setattr(hub_api.celery, "send_task", lambda *a, **k: None)
    body = {"spoke_org_id": env["brokered_org"], "kind": "ai",
            "speciality": "general-webapp",
            "description": "A tiny web app for the hub pass-through test",
            "hub_ref": f"hubp-{uuid.uuid4().hex[:8]}"}
    body.update(over)
    return client.post("/api/hub/projects", headers=_h(env["token"]), json=body)


# ---- create ----

def test_create_hub_project(client, env, monkeypatch):
    r = _create(client, env, monkeypatch)
    assert r.status_code == 201, r.text
    p = r.json()
    with SyncSession() as db:
        row = db.get(Project, p["id"])
        assert row.source == "hub" and row.hub_ref
        assert row.org_id == env["brokered_org"]
        # opaque subdomain: uuid prefix + fixed word, never description-derived
        assert row.subdomain == f"{row.id.split('-')[0]}-project"
        assert row.ssh_public_key and row.workspace_path
        # userless org: the spoke never emails or logs in this customer
        assert db.execute(select(User).where(
            User.org_id == env["brokered_org"])).scalars().all() == []


# ---- connected repos (§hub shared repo) ----

REPO_URI = "git@gitlab.example.com:engagements/e-1234.git"


def test_create_with_repos_binds_push_target_and_skips_platform_repo(client, env, monkeypatch):
    r = _create(client, env, monkeypatch,
                repos=[{"ssh_uri": REPO_URI},
                       {"ssh_uri": "git@github.com:acme/context.git"}],
                auto_merge=True)
    assert r.status_code == 201, r.text
    p = r.json()
    assert [x["ssh_uri"] for x in p["repos"]] == [REPO_URI, "git@github.com:acme/context.git"]
    with SyncSession() as db:
        rows = db.execute(select(ProjectRepo).where(ProjectRepo.project_id == p["id"])
                          .order_by(ProjectRepo.role)).scalars().all()
        assert [(x.role, x.is_push_target, x.auto_merge, x.provider) for x in rows] == [
            ("primary", True, True, "gitlab"), ("secondary", False, False, "github")]
        project = db.get(Project, p["id"])
        assert project.ssh_public_key  # its own deploy key, for the shared repo

    # The provisioning worker skips the platform-GitLab half for a project with a
    # connected push target: the connected repo is where the work lives.
    from app.workers import tasks as worker_tasks
    called = []
    monkeypatch.setattr(worker_tasks.gitlab, "create_project",
                        lambda *a, **k: called.append(a) or {"id": 1})
    worker_tasks.provision_project(p["id"], "")
    assert called == []
    with SyncSession() as db:
        assert db.get(Project, p["id"]).gitlab_project_id is None


def test_create_repo_gates(client, env, monkeypatch):
    # from_scratch=false without a repo, non-SSH remotes, and repos on non-ai kinds
    # are all refused before anything is created.
    assert _create(client, env, monkeypatch, from_scratch=False).status_code == 400
    assert _create(client, env, monkeypatch,
                   repos=[{"ssh_uri": "https://github.com/acme/x.git"}]).status_code == 400
    assert _create(client, env, monkeypatch, kind="chat", speciality=None,
                   repos=[{"ssh_uri": REPO_URI}]).status_code == 400


def test_save_run_ships_dev_state_to_the_outbox(client, env, monkeypatch):
    r = _create(client, env, monkeypatch)
    pid = r.json()["id"]
    from app.workers import tasks as worker_tasks
    with SyncSession() as db:
        project = db.get(Project, pid)
        db.execute(delete(HubProjectEvent).where(HubProjectEvent.project_id == pid))
        worker_tasks._save_run(project, "failed", error="boom")
        db.commit()
        events = db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == pid,
            HubProjectEvent.etype == "demo")).scalars().all()
        assert [e.payload.get("dev_run_state") for e in events] == ["failed"]
        # Same state again: no event storm - only CHANGES ship.
        worker_tasks._save_run(project, "failed", error="boom")
        db.commit()
        assert len(db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == pid,
            HubProjectEvent.etype == "demo")).scalars().all()) == 1


def test_a_consultants_quote_ships_to_the_hub(client, env, monkeypatch):
    """A direct_quote project's evaluation deliberately estimates nothing, so this
    price is the only one the hub will ever have. Without the event it waits forever
    for a figure only a person can give."""
    from app.services import hub_events
    pid = _create(client, env, monkeypatch, kind="direct_quote").json()["id"]
    with SyncSession() as db:
        db.execute(delete(HubProjectEvent).where(HubProjectEvent.project_id == pid))
        project = db.get(Project, pid)
        quote = Quote(project_id=pid, title="Playtesting pass", amount=140.0,
                      currency="credits", price_credits=140.0, status="sent")
        db.add(quote)
        db.flush()
        hub_events.record(db, project, "quote", hub_events.quote_payload(quote))
        db.commit()
        events = db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == pid,
            HubProjectEvent.etype == "quote")).scalars().all()
    assert len(events) == 1
    assert events[0].payload["price_credits"] == 140.0
    assert events[0].payload["status"] == "sent"
    assert events[0].payload["title"] == "Playtesting pass"
    with SyncSession() as db:      # module teardown drops projects; quotes FK them
        db.execute(delete(Quote).where(Quote.project_id == pid))
        db.commit()


def test_a_quote_on_a_direct_customers_project_ships_nothing(client, env, monkeypatch):
    """`record` is a no-op off hub projects, so the call site stays unconditional and a
    direct customer's pricing never leaves this system."""
    from app.services import hub_events
    with SyncSession() as db:
        org = db.execute(select(Organization)).scalars().first()
        own = Project(org_id=org.id, kind="direct_quote", status="draft",
                      name="Sold directly",
                      description="A project this consultant sold directly")
        db.add(own)
        db.flush()
        quote = Quote(project_id=own.id, title="Direct", amount=10.0,
                      currency="credits", price_credits=10.0, status="sent")
        db.add(quote)
        db.flush()
        hub_events.record(db, own, "quote", hub_events.quote_payload(quote))
        db.commit()
        assert db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == own.id)).scalars().all() == []
        db.execute(delete(Quote).where(Quote.project_id == own.id))
        db.execute(delete(Project).where(Project.id == own.id))
        db.commit()


def test_repo_check_route(client, env, monkeypatch):
    r = _create(client, env, monkeypatch, repos=[{"ssh_uri": REPO_URI}])
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    from app.api import hub as hub_api
    monkeypatch.setattr(hub_api.repolib, "check_ssh",
                        lambda uri, key: (True, f"Reachable: {uri}"))
    out = client.post(f"/api/hub/projects/{pid}/repos/check",
                      headers=_h(env["token"])).json()
    assert out == {"ok": True, "detail": f"Reachable: {REPO_URI}"}

    # A platform-repo project has nothing to check and says so.
    plain = _create(client, env, monkeypatch)
    out = client.post(f"/api/hub/projects/{plain.json()['id']}/repos/check",
                      headers=_h(env["token"])).json()
    assert out["ok"] is False and "platform repo" in out["detail"]


def test_create_rejected_outside_hub_managed_org(client, env):
    r = _create(client, env, spoke_org_id=env["direct_org"])
    assert r.status_code == 403


def test_create_respects_deposit_pause(client, env, monkeypatch):
    from app.services import app_settings as aps
    from app.api import hub as hub_api
    monkeypatch.setattr(hub_api.app_settings, "is_kind_paused", lambda flags, kind: True)
    r = _create(client, env)
    assert r.status_code == 403 and r.json()["detail"] == "deposits_paused"


def test_create_blocked_on_unpaid_balance(client, env, monkeypatch):
    # An outstanding (negative) balance from earlier work blocks a NEW project
    # until it is settled - the soft-limit extension.
    with SyncSession() as db:
        db.get(Organization, env["brokered_org"]).credit_balance = -12.0
        db.commit()
    r = _create(client, env)  # gate fires before provisioning
    assert r.status_code == 402, r.text
    assert "unpaid balance" in r.json()["detail"].lower()
    # settling it (balance back to >= 0) lets creation proceed again
    with SyncSession() as db:
        db.get(Organization, env["brokered_org"]).credit_balance = 0.0
        db.commit()
    assert _create(client, env, monkeypatch).status_code == 201


# ---- the hard boundary ----

def test_hub_token_cannot_see_customer_projects(client, env):
    h = _h(env["token"])
    pid = env["direct_project"]
    assert client.get(f"/api/hub/projects/{pid}", headers=h).status_code == 404
    assert client.get(f"/api/hub/projects/{pid}/messages", headers=h).status_code == 404
    assert client.get(f"/api/hub/projects/{pid}/dev-activity", headers=h).status_code == 404
    assert client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                       json={"action": "evaluate"}).status_code == 404


def test_org_id_mismatch_is_403(client, env, monkeypatch):
    pid = _create(client, env, monkeypatch).json()["id"]
    r = client.get(f"/api/hub/projects/{pid}", headers=_h(env["token"]),
                   params={"org_id": env["direct_org"]})
    assert r.status_code == 403


# ---- actions through the shared service layer ----

def test_evaluate_and_submit_actions(client, env, monkeypatch):
    sent: list = []
    from app.api import hub as hub_api
    monkeypatch.setattr(hub_api.celery, "send_task",
                        lambda name, args=None, **k: sent.append(name) or
                        type("T", (), {"id": "task-1"})())
    from app.services import project_actions as pa
    monkeypatch.setattr(pa.celery, "send_task",
                        lambda name, args=None, **k: sent.append(name) or
                        type("T", (), {"id": "task-1"})())
    pid = _create(client, env).json()["id"]
    h = _h(env["token"])
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "evaluate"})
    assert r.status_code == 200 and r.json()["task_id"] == "task-1"
    assert "app.workers.tasks.evaluate_project" in sent
    # submit blocked until the evaluation verdict allows it - same guard as the SPA
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "submit"})
    assert r.status_code == 409
    with SyncSession() as db:
        p = db.get(Project, pid)
        p.evaluation = {"state": "done", "feasibility": {"verdict": "pass"}}
        db.commit()
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "submit"})
    assert r.status_code == 200
    assert r.json()["project"]["status"] == "awaiting_review"


# ---- the transactional outbox ----

def test_hub_project_events_written_in_transaction(client, env, monkeypatch):
    from app.services import project_actions as pa
    monkeypatch.setattr(pa.celery, "send_task",
                        lambda *a, **k: type("T", (), {"id": "t"})())
    pid = _create(client, env, monkeypatch).json()["id"]
    with SyncSession() as db:
        from app.services.lifecycle import transition_sync
        p = db.get(Project, pid)
        p.evaluation = {"state": "done", "feasibility": {"verdict": "pass"}}
        transition_sync(db, p, "awaiting_review", "customer", "test submit")
        db.commit()
        rows = db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == pid).order_by(
            HubProjectEvent.created_at)).scalars().all()
        etypes = [r.etype for r in rows]
        assert "status" in etypes and "message" in etypes
        status_ev = next(r for r in rows if r.etype == "status")
        assert status_ev.payload["to"] == "awaiting_review"
        assert status_ev.hub_ref  # echoed for hub-side correlation
        assert all(r.sent_at is None for r in rows)


def test_customer_projects_never_feed_the_outbox(env):
    with SyncSession() as db:
        from app.services.lifecycle import transition_sync
        p = db.get(Project, env["direct_project"])
        transition_sync(db, p, "awaiting_customer", "agent", "test")
        db.commit()
        assert db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == p.id)).scalars().all() == []
        # cleanup the message/status rows the transition wrote
        db.execute(delete(Message).where(Message.project_id == p.id))
        db.execute(delete(StatusChange).where(StatusChange.project_id == p.id))
        db.commit()


def test_outbox_push_marks_sent_only_on_full_ack(client, env, monkeypatch):
    from app.workers import hub as hub_worker
    from app.services import project_actions as pa
    monkeypatch.setattr(pa.celery, "send_task",
                        lambda *a, **k: type("T", (), {"id": "t"})())
    pid = _create(client, env, monkeypatch).json()["id"]
    with SyncSession() as db:
        from app.services.lifecycle import transition_sync
        p = db.get(Project, pid)
        p.evaluation = {"state": "done", "feasibility": {"verdict": "pass"}}
        transition_sync(db, p, "awaiting_review", "customer", "push test")
        db.commit()
    monkeypatch.setattr(hub_worker.settings, "hub_mcp_url", "http://hub.test/mcp")
    # partial ack -> nothing marked sent
    monkeypatch.setattr(hub_worker.hub_client, "report_project_events",
                        lambda events: {"acked": len(events) - 1})
    hub_worker.hub_project_events_report()
    with SyncSession() as db:
        unsent = db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == pid,
            HubProjectEvent.sent_at.is_(None))).scalars().all()
        assert len(unsent) >= 2
    # full ack -> all marked sent
    shipped: list = []
    monkeypatch.setattr(hub_worker.hub_client, "report_project_events",
                        lambda events: shipped.extend(events) or {"acked": len(events)})
    hub_worker.hub_project_events_report()
    with SyncSession() as db:
        assert db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == pid,
            HubProjectEvent.sent_at.is_(None))).scalars().all() == []
    assert any(e["project_id"] == pid and e["etype"] == "status" for e in shipped)


def test_push_noop_without_hub(monkeypatch):
    from app.workers import hub as hub_worker
    monkeypatch.setattr(hub_worker.settings, "hub_mcp_url", "")
    monkeypatch.setattr(hub_worker.hub_client, "report_project_events",
                        lambda events: pytest.fail("must not be called"))
    assert hub_worker.hub_project_events_report() is None

# ---- P2 interactivity ----

def test_post_message_and_requests_flow(client, env, monkeypatch):
    sent: list = []
    from app.api import hub as hub_api
    from app.services import project_actions as pa
    fake = lambda name, args=None, **k: sent.append(name) or type("T", (), {"id": "t"})()
    monkeypatch.setattr(hub_api.celery, "send_task", fake)
    monkeypatch.setattr(pa.celery, "send_task", fake)
    pid = _create(client, env).json()["id"]
    h = _h(env["token"])

    r = client.post(f"/api/hub/projects/{pid}/messages", headers=h,
                    json={"body": "Please make the header blue."})
    assert r.status_code == 201 and r.json()["author"] == "customer"
    # the §12 classifier ran exactly as it does for the SPA
    assert "app.workers.tasks.classify_chat_message" in sent
    msgs = client.get(f"/api/hub/projects/{pid}/messages", headers=h).json()
    assert any(m["body"] == "Please make the header blue." for m in msgs)

    r = client.post(f"/api/hub/projects/{pid}/requests", headers=h,
                    json={"type": "feature", "handling": "ai",
                          "body": "Add a dark mode toggle"})
    assert r.status_code == 201, r.text
    req = r.json()
    assert "app.workers.tasks.handle_request" in sent
    rows = client.get(f"/api/hub/projects/{pid}/requests", headers=h).json()
    assert any(x["id"] == req["id"] for x in rows)
    # the request's own thread is a valid message thread on the hub surface too
    r = client.post(f"/api/hub/projects/{pid}/messages", headers=h,
                    json={"thread": f"request:{req['id']}", "body": "extra detail"})
    assert r.status_code == 201
    with SyncSession() as db:
        # every human message landed in the outbox transactionally
        etypes = [e.etype for e in db.execute(select(HubProjectEvent).where(
            HubProjectEvent.project_id == pid)).scalars().all()]
        assert etypes.count("message") >= 3


def test_memory_passthrough_crud(client, env, monkeypatch):
    pid = _create(client, env, monkeypatch).json()["id"]
    h = _h(env["token"])
    r = client.put(f"/api/hub/projects/{pid}/memory", headers=h,
                   json={"key": "GITHUB_TOKEN", "value": "ghp_test123",
                         "is_secret": True, "description": "repo token"})
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["author"] == "customer" and entry["value"] == "ghp_test123"
    rows = client.get(f"/api/hub/projects/{pid}/memory", headers=h).json()
    assert len(rows) == 1 and rows[0]["is_secret"] is True
    r = client.delete(f"/api/hub/projects/{pid}/memory/{entry['id']}", headers=h)
    assert r.status_code == 200
    assert client.get(f"/api/hub/projects/{pid}/memory", headers=h).json() == []


def test_extended_actions_guards(client, env, monkeypatch):
    pid = _create(client, env, monkeypatch).json()["id"]
    h = _h(env["token"])
    # same guards as the SPA: no demo yet -> approve-delivery 409; no run -> stop 409
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "approve-delivery"})
    assert r.status_code == 409
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "stop-build"})
    assert r.status_code == 409
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "not-a-thing"})
    assert r.status_code == 422  # schema-rejected


def test_demo_actions_wrap_the_spa_guards(client, env, monkeypatch):
    """§pass-through: demo-start/demo-stop run the SAME project_actions guards
    as the SPA demo routes - a fresh draft can't deploy (409), a stopped demo
    can't stop (409), and a deployable project's start dispatches demo_start."""
    from app.services import project_actions as pa

    pid = _create(client, env, monkeypatch).json()["id"]
    h = _h(env["token"])
    sent = []
    monkeypatch.setattr(pa.celery, "send_task", lambda *a, **k: sent.append(a))
    # draft project: not deployable yet
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "demo-start"})
    assert r.status_code == 409
    # stopped demo: nothing to stop
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "demo-stop"})
    assert r.status_code == 409
    assert sent == []
    with SyncSession() as db:
        db.query(Project).filter_by(id=pid).update({"status": "development"})
        db.commit()
    r = client.post(f"/api/hub/projects/{pid}/actions", headers=h,
                    json={"action": "demo-start"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sent and sent[0][0] == "app.workers.tasks.demo_start"


def test_interactive_boundary_still_holds(client, env):
    h = _h(env["token"])
    pid = env["direct_project"]
    assert client.post(f"/api/hub/projects/{pid}/messages", headers=h,
                       json={"body": "x"}).status_code == 404
    assert client.get(f"/api/hub/projects/{pid}/requests", headers=h).status_code == 404
    assert client.get(f"/api/hub/projects/{pid}/memory", headers=h).status_code == 404
