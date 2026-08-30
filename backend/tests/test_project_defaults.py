"""§project defaults: the per-kind knowledge-base, tool and model defaults
(/admin/settings).

The two gates default opposite ways - a KB reaches a project only if `kb_ids`
names it, a §Tools row reaches every project unless a `ProjectToolConfig` says
otherwise - so the defaults are stored as a SELECTION for knowledge bases and an
EXCLUSION list for tools, and the tests pin both halves plus the stamp-time drop
of an id whose row was deleted (a dangling tool_id would be a foreign-key error
on a customer's create click).

The model default is the odd one out and is pinned as such: it is NOT stamped at
creation, it is a link in `model_config`'s resolution chain, so changing it moves
every project of that kind that never chose its own - and everything keyed on
WHICH endpoint answers (reasoning effort, the §chat images verdict) has to follow
it through the same resolver.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.core.security import hash_password
from app.main import app
from app.models import (
    AppSetting, CreditTransaction, KnowledgeBase, Message, ModelEndpoint, Organization,
    Project, ProjectModelConfig, ProjectToolConfig, Request as RequestRow, StatusChange,
    Tool, User,
)
from app.seed import seed_knowledge_bases
from app.core.config import settings
from app.services import events, model_config, project_defaults, vision


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


# --------------------------------------------------------- the per-kind model


@pytest.fixture
def endpoints():
    """Two saved endpoints to route kinds at, cleaned up after each test."""
    with SyncSession() as db:
        db.add_all([
            ModelEndpoint(label="Cheap chat", provider="custom", base_url="https://a.example/v1",
                          api_key_enc=encrypt("key-a"), model_name="cheap-1",
                          reasoning_effort="low", supports_images=True),
            ModelEndpoint(label="Build model", provider="custom", base_url="https://b.example/v1",
                          api_key_enc=encrypt("key-b"), model_name="strong-1"),
        ])
        db.commit()
        a, b = db.execute(select(ModelEndpoint.id).where(
            ModelEndpoint.label.in_(("Cheap chat", "Build model")))
            .order_by(ModelEndpoint.label)).scalars().all()  # Build model, Cheap chat
        ids = {"build": a, "chat": b}
    yield ids
    with SyncSession() as db:
        db.execute(delete(ProjectModelConfig).where(
            ProjectModelConfig.endpoint_id.in_(tuple(ids.values()))))
        db.execute(delete(ModelEndpoint).where(ModelEndpoint.id.in_(tuple(ids.values()))))
        db.execute(delete(AppSetting).where(AppSetting.key == model_config.KIND_DEFAULT_KEY))
        db.commit()


def _project(db, kind):
    org = db.execute(select(Organization)).scalars().first()
    p = Project(org_id=org.id, name="resolution", description="d", kind=kind)
    db.add(p)
    db.flush()
    return p


def test_kind_model_default_resolves_per_call(client, admin, endpoints):
    """The kind default is a LINK in the chain, not a stamp: a chat project that
    never chose a model follows it (and follows a later change), an `ai` project is
    untouched, and a project with its own endpoint keeps it."""
    h, _ = admin
    assert client.put("/api/admin/settings", headers=h, json={
        "default_model_endpoints": {"chat": endpoints["chat"]}}).status_code == 200

    with SyncSession() as db:
        chat, ai = _project(db, "chat"), _project(db, "ai")
        assert model_config.project_model_config(db, chat)[2] == "cheap-1"
        assert model_config.project_model_config(db, ai)[2] == settings.openai_model

        # everything keyed on WHICH endpoint answers follows the same resolution
        from app.workers.tasks import _project_reasoning_effort
        assert _project_reasoning_effort(db, chat) == "low"     # the endpoint's
        assert _project_reasoning_effort(db, ai) == "high"      # the dev default
        assert vision.project_image_support_sync(db, chat)["enabled"] is True
        assert vision.project_image_support_sync(db, ai)["enabled"] is False

        # a project pinned on the project page keeps what it was given
        db.add(ProjectModelConfig(project_id=chat.id, endpoint_id=endpoints["build"]))
        db.flush()
        assert model_config.project_model_config(db, chat)[2] == "strong-1"
        db.rollback()


def test_kind_model_default_survives_a_deleted_endpoint(client, admin, endpoints):
    """An endpoint deleted out from under the setting degrades to the instance
    default instead of failing every call."""
    _store(model_config.KIND_DEFAULT_KEY, {"chat": "gone-endpoint-id"})
    with SyncSession() as db:
        chat = _project(db, "chat")
        assert model_config.project_model_config(db, chat)[2] == settings.openai_model
        assert vision.project_image_support_sync(db, chat)["model"] == settings.openai_model
        db.rollback()


def test_kind_model_default_validation(client, admin, endpoints):
    h, _ = admin
    assert client.put("/api/admin/settings", headers=h, json={
        "default_model_endpoints": {"chat": "nope"}}).status_code == 422
    assert client.put("/api/admin/settings", headers=h, json={
        "default_model_endpoints": {"mcp": endpoints["chat"]}}).status_code == 422

    # an endpoint with no model can't name what it runs - refused like the
    # per-project route refuses it
    with SyncSession() as db:
        db.add(ModelEndpoint(label="No model", provider="custom", base_url="https://c.example/v1",
                             api_key_enc=encrypt("k")))
        db.commit()
        modelless = db.execute(select(ModelEndpoint.id).where(
            ModelEndpoint.label == "No model")).scalar_one()
    try:
        assert client.put("/api/admin/settings", headers=h, json={
            "default_model_endpoints": {"chat": modelless}}).status_code == 422
    finally:
        with SyncSession() as db:
            db.execute(delete(ModelEndpoint).where(ModelEndpoint.id == modelless))
            db.commit()

    # "" clears a kind back to the instance default
    assert client.put("/api/admin/settings", headers=h, json={
        "default_model_endpoints": {"chat": endpoints["chat"]}}).status_code == 200
    out = client.put("/api/admin/settings", headers=h, json={
        "default_model_endpoints": {"chat": ""}}).json()
    assert out["default_model_endpoints"] == {k: None for k in project_defaults.KINDS}
