"""§project defaults: the per-kind knowledge-base and tool selection a NEW project
is created with (/admin/settings).

The two gates default opposite ways - a KB reaches a project only if `kb_ids`
names it, a §Tools row reaches every project unless a `ProjectToolConfig` says
otherwise - so the defaults are stored as a SELECTION for knowledge bases and an
EXCLUSION list for tools, and the tests pin both halves plus the stamp-time drop
of an id whose row was deleted (a dangling tool_id would be a foreign-key error
on a customer's create click).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import (
    AppSetting, CreditTransaction, KnowledgeBase, Message, Organization, Project,
    ProjectToolConfig, Request as RequestRow, StatusChange, Tool, User,
)
from app.seed import seed_knowledge_bases
from app.services import events, project_defaults


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    try:
        with TestClient(app) as c:
            yield c
    finally:
        # creating a chat project publishes WS events, binding the shared async
        # redis client to THIS TestClient's loop - drop it for later modules.
        asyncio.run(engine.dispose(close=False))
        events._async_client = None


@pytest.fixture(scope="module")
def admin(client):
    """One admin session for the whole module (the login limiter is shared per
    test-client IP), on an org with credits - the admin creates the projects here
    and a chat project debits its opening fee in the create transaction."""
    email = f"defaults-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "defaults-secret-12"
    with SyncSession() as db:
        org = Organization(name="Defaults Org", credit_balance=500.0)
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="admin", email_verified=True))
        db.commit()
        oid = org.id
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    yield {"X-CSRF-Token": tok}, oid
    _cleanup_org(oid)


@pytest.fixture(autouse=True)
def rows():
    """The seeded KBs plus two tool rows to select between; both maps are cleared
    around every test so one never leaks its defaults into the next."""
    seed_knowledge_bases()
    _clear_defaults()
    with SyncSession() as db:
        db.add_all([
            Tool(slug=f"t-{uuid.uuid4().hex[:6]}", name="Tool A", kind="github",
                 url="https://a.example/mcp", enabled=True),
            Tool(slug=f"t-{uuid.uuid4().hex[:6]}", name="Tool B", kind="gitlab",
                 url="https://b.example/mcp", enabled=True),
        ])
        db.commit()
        tool_a, tool_b = db.execute(select(Tool.id).where(
            Tool.name.in_(("Tool A", "Tool B"))).order_by(Tool.name)).scalars().all()
        local_id = db.execute(select(KnowledgeBase.id).where(
            KnowledgeBase.kind == "local")).scalar_one()
    yield {"tool_a": tool_a, "tool_b": tool_b, "local": local_id}
    with SyncSession() as db:
        db.execute(delete(ProjectToolConfig).where(
            ProjectToolConfig.tool_id.in_((tool_a, tool_b))))
        db.execute(delete(Tool).where(Tool.id.in_((tool_a, tool_b))))
        db.commit()
    _clear_defaults()


def _clear_defaults():
    with SyncSession() as db:
        db.execute(delete(AppSetting).where(AppSetting.key.in_(
            (project_defaults.KB_KEY, project_defaults.TOOLS_OFF_KEY))))
        db.commit()


def _store(key, value):
    """Write a defaults map straight to the setting, bypassing the route's id
    validation - the only way to reproduce a row deleted after it was selected."""
    with SyncSession() as db:
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
        db.commit()


def _cleanup_org(oid):
    with SyncSession() as db:
        pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
        if pids:
            db.execute(delete(ProjectToolConfig).where(ProjectToolConfig.project_id.in_(pids)))
            db.execute(delete(Message).where(Message.project_id.in_(pids)))
            db.execute(delete(RequestRow).where(RequestRow.project_id.in_(pids)))
            db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
        db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
        if pids:
            db.execute(delete(Project).where(Project.id.in_(pids)))
        db.execute(delete(User).where(User.org_id == oid))
        db.execute(delete(Organization).where(Organization.id == oid))
        db.commit()


def _create(client, headers, kind, description):
    r = client.post("/api/projects", headers=headers, json={
        "kind": kind, "description": description, "from_scratch": True,
        "sovereign": False, **({"speciality": "sovereign-ca"} if kind == "ai" else {})})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_settings_roundtrip_and_validation(client, admin, rows):
    h, _ = admin

    # unconfigured: every kind present and empty - today's behavior on both gates
    # (no knowledge bases selected, no tool switched off).
    out = client.get("/api/admin/settings", headers=h).json()
    assert set(out["default_kb_ids"]) == set(project_defaults.KINDS)
    assert all(v == [] for v in out["default_kb_ids"].values())
    assert all(v == [] for v in out["default_tools_off"].values())

    r = client.put("/api/admin/settings", headers=h, json={
        "default_kb_ids": {"chat": [rows["local"]], "ai": []},
        "default_tools_off": {"chat": [rows["tool_a"]]}})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["default_kb_ids"]["chat"] == [rows["local"]]
    assert saved["default_kb_ids"]["ai"] == []
    assert saved["default_tools_off"]["chat"] == [rows["tool_a"]]
    # a kind the map never mentioned still reads as "no defaults"
    assert saved["default_kb_ids"]["auto_dev"] == []

    # an id or a kind that doesn't exist is refused - it would otherwise become a
    # dangling override at someone's create click
    assert client.put("/api/admin/settings", headers=h, json={
        "default_kb_ids": {"chat": ["not-a-kb"]}}).status_code == 422
    assert client.put("/api/admin/settings", headers=h, json={
        "default_tools_off": {"chat": ["not-a-tool"]}}).status_code == 422
    assert client.put("/api/admin/settings", headers=h, json={
        "default_kb_ids": {"mcp": []}}).status_code == 422
    # the refusals left the stored maps alone
    out = client.get("/api/admin/settings", headers=h).json()
    assert out["default_kb_ids"]["chat"] == [rows["local"]]


def test_project_starts_with_its_own_kinds_defaults(client, admin, rows, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    h, _ = admin

    # chat reads the knowledge base and may not call Tool A; ai gets neither the
    # selection nor the exclusion - the two kinds are separate decisions.
    assert client.put("/api/admin/settings", headers=h, json={
        "default_kb_ids": {"chat": [rows["local"]], "ai": []},
        "default_tools_off": {"chat": [rows["tool_a"]], "ai": []},
    }).status_code == 200

    chat_id = _create(client, h, "chat", "Tell me about sovereign clouds.")
    ai_id = _create(client, h, "ai", "Build me a small internal tool.")

    with SyncSession() as db:
        chat = db.get(Project, chat_id)
        assert chat.kb_ids == [rows["local"]]
        offs = db.execute(select(ProjectToolConfig).where(
            ProjectToolConfig.project_id == chat_id)).scalars().all()
        # only the excluded tool gets a row; everything else keeps inheriting, so a
        # tool added to the instance later still reaches this project
        assert [(o.tool_id, o.enabled) for o in offs] == [(rows["tool_a"], False)]

        ai = db.get(Project, ai_id)
        assert ai.kb_ids == []
        assert db.execute(select(ProjectToolConfig).where(
            ProjectToolConfig.project_id == ai_id)).scalars().all() == []


def test_deleted_row_is_dropped_at_creation(client, admin, rows, monkeypatch):
    """A KB or tool removed after it was defaulted must not turn the customer's
    create click into a foreign-key error."""
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    h, _ = admin
    _store(project_defaults.KB_KEY, {"chat": [rows["local"], "deleted-kb"]})
    _store(project_defaults.TOOLS_OFF_KEY, {"chat": [rows["tool_b"], "deleted-tool"]})

    chat_id = _create(client, h, "chat", "Another conversation, please.")
    with SyncSession() as db:
        assert db.get(Project, chat_id).kb_ids == [rows["local"]]
        offs = db.execute(select(ProjectToolConfig).where(
            ProjectToolConfig.project_id == chat_id)).scalars().all()
        assert [o.tool_id for o in offs] == [rows["tool_b"]]
