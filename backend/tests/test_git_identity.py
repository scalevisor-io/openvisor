"""§git identity: the per-project git author (user.name / user.email) the agent
commits as. Resolution (override vs brand-derived default), the PATCH surface and
its validation, and the one thing that makes the setting real - the dispatcher
forwarding it to the runner job.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.core.security import hash_password
from app.main import app
from app.models import Message, Organization, Project, ProjectRepo, StatusChange, User
from app.services import deployer_client, repos as repolib
from app.workers import tasks


# ---------------------------------------------------------------- resolution

def test_default_identity_follows_the_brand(monkeypatch):
    monkeypatch.setattr(repolib.settings, "brand_name", "acme.ai")
    monkeypatch.setattr(repolib.settings, "deploy_domain", "acme.example")
    assert repolib.default_git_identity() == ("acme.ai agent", "agent@acme.example")


def test_git_identity_prefers_the_project_override(monkeypatch):
    monkeypatch.setattr(repolib.settings, "brand_name", "acme.ai")
    monkeypatch.setattr(repolib.settings, "deploy_domain", "acme.example")
    p = Project(org_id="o", name="P", description="d")
    assert repolib.git_identity(p) == ("acme.ai agent", "agent@acme.example")
    p.git_author_name = "Acme Bot"
    assert repolib.git_identity(p) == ("Acme Bot", "agent@acme.example")
    p.git_author_email = "bot@acme.example"
    assert repolib.git_identity(p) == ("Acme Bot", "bot@acme.example")
    # a whitespace-only override is not an identity - it falls back
    p.git_author_name = "   "
    assert repolib.git_identity(p)[0] == "acme.ai agent"


# ---------------------------------------------------------------- dispatch

def test_dispatch_forwards_the_identity_to_the_runner(monkeypatch):
    """The setting only exists if the runner receives it: pin the forwarding."""
    sent = {}
    monkeypatch.setattr(tasks, "_project_model_config",
                        lambda db, p: ("http://llm", "k", "m"))
    monkeypatch.setattr(tasks, "_prepare_runner_inputs",
                        lambda *a, **k: None)
    monkeypatch.setattr(tasks.dev_concurrency, "bound_run", lambda p: None)
    monkeypatch.setattr(tasks, "_project_reasoning_effort", lambda db, p: "")
    monkeypatch.setattr(tasks.egress, "is_enabled", lambda db: False)  # §egress off
    # §dev harness: resolved from the db like the egress flag above, and these
    # dispatch tests run without one - pin the default driver.
    monkeypatch.setattr(tasks.dev_harness, "resolve",
                        lambda db, p: tasks.dev_harness.HARNESSES["openhands"])
    monkeypatch.setattr(tasks.deployer_client, "run_dev_job",
                        lambda *a, **kw: sent.update(kw) or {"ok": True})

    project = Project(org_id="o", name="P", description="d", kind="ai")
    project.git_author_name = "Acme Bot"
    project.git_author_email = "bot@acme.example"
    target = {"provider": "gitlab", "remote": "git@gitlab.com:o/r.git",
              "base_branch": "main", "runner_provider": "gitlab"}
    tasks._dispatch_runner(None, project, target)
    assert sent["git_author_name"] == "Acme Bot"
    assert sent["git_author_email"] == "bot@acme.example"
    assert sent["brand_name"] == tasks.settings.brand_name


def test_run_dev_job_puts_the_identity_in_the_body(monkeypatch):
    body = {}
    monkeypatch.setattr(deployer_client, "_call",
                        lambda method, path, payload, timeout=0: body.update(payload) or {})
    deployer_client.run_dev_job("pid", llm_model="m", llm_api_key="k", llm_base_url="u",
                                git_author_name="Acme Bot", git_author_email="bot@acme.example",
                                brand_name="Acme")
    assert body["git_author_name"] == "Acme Bot"
    assert body["git_author_email"] == "bot@acme.example"
    assert body["brand_name"] == "Acme"


# ---------------------------------------------------------------- HTTP

@pytest.fixture(scope="module")
def client():
    # Module-scoped and entered as a context manager so every request shares one
    # persistent event loop. Dispose the async pool on the way OUT (close=False
    # abandons rather than awaits): the connections are bound to this module's
    # loop, and the next HTTP module would inherit them closed.
    import asyncio

    from app.core.db import engine
    with TestClient(app) as c:
        yield c
    asyncio.run(engine.dispose(close=False))


@pytest.fixture(scope="module")
def org():
    with SyncSession() as db:
        o = Organization(name="Git Identity Org", credit_balance=100.0)
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
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
                db.execute(delete(Project).where(Project.id.in_(pids)))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _project(org_id) -> str:
    with SyncSession() as db:
        p = Project(org_id=org_id, name="GI", description="d", kind="ai", status="draft",
                    ssh_public_key="ssh-ed25519 AAAA test", ssh_private_key_enc=encrypt("K"))
        db.add(p)
        db.commit()
        return p.id


@pytest.fixture(scope="module")
def auth(org, client):
    """One login for the whole module: the login limiter is per-IP and shared by
    the suite, so a login per test 429s its neighbours."""
    email = f"gitid-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "customer-secret-123"
    with SyncSession() as db:
        db.add(User(org_id=org, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        db.commit()
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_identity_endpoint_flow(org, client, auth):
    pid, h = _project(org), auth

    # a fresh project inherits: no override, effective = the instance default
    p = client.get(f"/api/projects/{pid}", headers=h).json()
    default_name, default_email = repolib.default_git_identity()
    assert p["git_author_name"] is None and p["git_author_email"] is None
    assert p["git_author_name_effective"] == default_name
    assert p["git_author_email_effective"] == default_email

    # set both
    r = client.patch(f"/api/projects/{pid}",
                     json={"git_author_name": "Acme Bot",
                           "git_author_email": "bot@acme.example"}, headers=h)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["git_author_name"] == "Acme Bot"
    assert p["git_author_name_effective"] == "Acme Bot"
    assert p["git_author_email_effective"] == "bot@acme.example"

    # one field only: the other keeps its override
    p = client.patch(f"/api/projects/{pid}", json={"git_author_name": "Other Bot"},
                     headers=h).json()
    assert p["git_author_name"] == "Other Bot"
    assert p["git_author_email"] == "bot@acme.example"

    # "" resets to the instance default (stored as null, not an empty identity)
    p = client.patch(f"/api/projects/{pid}",
                     json={"git_author_name": "", "git_author_email": ""}, headers=h).json()
    assert p["git_author_name"] is None and p["git_author_email"] is None
    assert p["git_author_name_effective"] == default_name

    # an empty patch is still rejected
    assert client.patch(f"/api/projects/{pid}", json={}, headers=h).status_code == 400


@pytest.mark.parametrize("payload", [
    {"git_author_email": "not-an-email"},
    {"git_author_email": "bot@acme.example\nuser.name = intruder"},
    # git itself rejects <> in a name; refuse it at the door rather than at commit
    {"git_author_name": "Bot <bot@acme.example>"},
    {"git_author_name": "Bot\n[core]\n\thooksPath = /tmp"},
])
def test_identity_validation_rejects_injection(org, client, auth, payload):
    pid, h = _project(org), auth
    r = client.patch(f"/api/projects/{pid}", json=payload, headers=h)
    assert r.status_code == 422, r.text
    p = client.get(f"/api/projects/{pid}", headers=h).json()
    assert p["git_author_name"] is None and p["git_author_email"] is None
