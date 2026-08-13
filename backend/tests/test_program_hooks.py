"""§28 inbound trigger hooks: signature verification + event normalization +
filter matching (pure), the public receiver route (HMAC auth, replay dedup,
filters, pending cap, credit failure), and the worker-side serialization
(run_program's per-instance guard + the sweep's deferred dispatch).
"""
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.main import app
from app.models import (
    Organization, Program, ProgramInstance, ProgramRun, utcnow,
)
from app.services import events
from app.services import program_hooks as hs
from app.workers import programs as wp

_N = iter(range(10000))
SECRET = "hook-secret-for-tests"


def _gh_headers(body: bytes, secret: str = SECRET, event: str = "issues",
                delivery: str | None = None) -> dict:
    return {
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body,
                                                    hashlib.sha256).hexdigest(),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def _gh_issue_payload(action="labeled", labels=("run",), assignees=(),
                      author="alice", number=1) -> dict:
    return {"action": action, "issue": {
        "number": number, "html_url": f"https://github.com/a/r/issues/{number}",
        "title": "t", "body": "b",
        "labels": [{"name": l} for l in labels],
        "assignees": [{"login": a} for a in assignees],
        "user": {"login": author}}}


# ---------------------------------------------------------------- pure functions

def test_verify_signature_github_and_gitlab():
    body = b'{"x":1}'
    gh = _gh_headers(body)
    assert hs.verify_signature(SECRET, {k.lower(): v for k, v in gh.items()}, body) == "github"
    bad = dict(gh, **{"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert hs.verify_signature(SECRET, {k.lower(): v for k, v in bad.items()}, body) is None
    assert hs.verify_signature(SECRET, {"x-gitlab-token": SECRET}, body) == "gitlab"
    assert hs.verify_signature(SECRET, {"x-gitlab-token": "wrong"}, body) is None
    assert hs.verify_signature(SECRET, {}, body) is None


def test_normalize_event_shapes():
    p = _gh_issue_payload(labels=("run", "x"), assignees=("bot",))
    ev = hs.normalize_event("github", {"x-github-event": "issues"}, p)
    assert ev["action"] == "labeled" and ev["issue"]["labels"] == ["run", "x"]
    assert ev["issue"]["assignees"] == ["bot"] and ev["issue"]["author"] == "alice"
    assert hs.normalize_event("github", {"x-github-event": "push"}, p) is None

    gl = {"object_kind": "issue", "user": {"username": "actor"},
          "labels": [{"title": "run"}], "assignees": [{"username": "bot"}],
          "object_attributes": {"iid": 9, "url": "https://gl/i/9", "title": "t",
                                "description": "d", "action": "open"}}
    ev = hs.normalize_event("gitlab", {}, gl)
    assert ev["issue"]["iid"] == 9 and ev["issue"]["author"] == "actor"
    assert hs.normalize_event("gitlab", {}, {"object_kind": "push"}) is None


def test_event_matches_allowlists():
    ev = hs.normalize_event("github", {"x-github-event": "issues"},
                            _gh_issue_payload(labels=("run",), assignees=("bot",)))
    assert hs.event_matches({}, ev)  # no filters = accept (secret is the auth)
    assert hs.event_matches({"actions": ["labeled"], "labels": ["run"]}, ev)
    assert not hs.event_matches({"actions": ["opened"]}, ev)
    assert not hs.event_matches({"labels": ["other"]}, ev)
    assert hs.event_matches({"assignees": ["bot"]}, ev)
    assert not hs.event_matches({"authors": ["boss"]}, ev)


# ---------------------------------------------------------------- HTTP receiver

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


@pytest.fixture
def setup():
    """Committed org + published program + hook-enabled instance; torn down."""
    with SyncSession() as db:
        org = Organization(name="Hook Org", credit_balance=50.0)
        db.add(org)
        db.flush()
        program = Program(title="HookProg", gitlab_repo_path=f"grp/hook-{next(_N)}",
                          is_published=True, schedulable=True)
        db.add(program)
        db.flush()
        inst = ProgramInstance(program_id=program.id, org_id=org.id,
                               ssh_public_key="pk", ssh_private_key_enc="enc",
                               hook_enabled=True, hook_secret_enc=encrypt(SECRET),
                               hook_filters={"labels": ["run"]})
        db.add(inst)
        db.commit()
        ids = {"org": org.id, "program": program.id, "inst": inst.id}
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(ProgramRun).where(ProgramRun.instance_id == ids["inst"]))
            db.execute(delete(ProgramInstance).where(ProgramInstance.id == ids["inst"]))
            db.execute(delete(Program).where(Program.id == ids["program"]))
            db.execute(delete(Organization).where(Organization.id == ids["org"]))
            db.commit()


@pytest.fixture
def no_dispatch(monkeypatch):
    from app.workers.celery_app import celery
    sent: list = []
    monkeypatch.setattr(celery, "send_task", lambda *a, **k: sent.append(a))
    return sent


def _runs(inst_id):
    with SyncSession() as db:
        return db.execute(select(ProgramRun).where(
            ProgramRun.instance_id == inst_id)
            .order_by(ProgramRun.created_at)).scalars().all()


def test_receiver_enqueues_hook_run(client, setup, no_dispatch):
    body = json.dumps(_gh_issue_payload()).encode()
    r = client.post(f"/api/programs/hooks/{setup['inst']}", content=body,
                    headers=_gh_headers(body))
    assert r.status_code == 204, r.text
    runs = _runs(setup["inst"])
    assert len(runs) == 1
    assert runs[0].kind == "hook" and runs[0].state == "queued"
    assert runs[0].hook_event["issue"]["labels"] == ["run"]
    assert runs[0].hook_event["delivery"]
    assert no_dispatch and no_dispatch[0][0] == "app.workers.programs.run_program"


def test_receiver_replay_and_auth(client, setup, no_dispatch):
    body = json.dumps(_gh_issue_payload()).encode()
    h = _gh_headers(body)
    assert client.post(f"/api/programs/hooks/{setup['inst']}", content=body,
                       headers=h).status_code == 204
    # same delivery id again -> acknowledged, no second run
    assert client.post(f"/api/programs/hooks/{setup['inst']}", content=body,
                       headers=h).status_code == 204
    assert len(_runs(setup["inst"])) == 1
    # bad signature -> 401; unknown instance / disabled hook -> 404
    bad = dict(h, **{"X-Hub-Signature-256": "sha256=" + "0" * 64,
                     "X-GitHub-Delivery": str(uuid.uuid4())})
    assert client.post(f"/api/programs/hooks/{setup['inst']}", content=body,
                       headers=bad).status_code == 401
    assert client.post(f"/api/programs/hooks/{uuid.uuid4()}", content=body,
                       headers=_gh_headers(body)).status_code == 404
    with SyncSession() as db:
        db.get(ProgramInstance, setup["inst"]).hook_enabled = False
        db.commit()
    assert client.post(f"/api/programs/hooks/{setup['inst']}", content=body,
                       headers=_gh_headers(body)).status_code == 404


def test_receiver_filters_and_pending_cap(client, setup, no_dispatch, monkeypatch):
    # label filter miss -> acknowledged, nothing enqueued
    body = json.dumps(_gh_issue_payload(labels=("other",))).encode()
    assert client.post(f"/api/programs/hooks/{setup['inst']}", content=body,
                       headers=_gh_headers(body)).status_code == 204
    assert _runs(setup["inst"]) == []
    # pending cap: with N queued hook runs the next delivery is dropped
    from app.core.config import settings
    monkeypatch.setattr(settings, "program_hook_max_pending", 1)
    with SyncSession() as db:
        db.add(ProgramRun(program_id=setup["program"], instance_id=setup["inst"],
                          org_id=setup["org"], kind="hook"))
        db.commit()
    body = json.dumps(_gh_issue_payload()).encode()
    assert client.post(f"/api/programs/hooks/{setup['inst']}", content=body,
                       headers=_gh_headers(body)).status_code == 204
    assert len(_runs(setup["inst"])) == 1  # only the pre-seeded one


def test_receiver_out_of_credits_visible_failure(client, setup, no_dispatch):
    with SyncSession() as db:
        db.get(Organization, setup["org"]).credit_balance = 0.0
        db.commit()
    body = json.dumps(_gh_issue_payload()).encode()
    assert client.post(f"/api/programs/hooks/{setup['inst']}", content=body,
                       headers=_gh_headers(body)).status_code == 204
    runs = _runs(setup["inst"])
    assert len(runs) == 1 and runs[0].state == "failed"
    assert "credit" in (runs[0].error or "")
    assert no_dispatch == []  # nothing dispatched


# ---------------------------------------------------------------- worker side

def test_run_program_serializes_per_instance():
    with SyncSession() as db:
        org = Organization(name="Hook Org", credit_balance=50.0)
        db.add(org)
        db.flush()
        program = Program(title="P", gitlab_repo_path=f"grp/hook-{next(_N)}",
                          is_published=True)
        db.add(program)
        db.flush()
        inst = ProgramInstance(program_id=program.id, org_id=org.id,
                               ssh_public_key="pk", ssh_private_key_enc="enc")
        db.add(inst)
        db.flush()
        db.add(ProgramRun(program_id=program.id, instance_id=inst.id,
                          org_id=org.id, kind="manual", state="running",
                          started_at=utcnow()))
        queued = ProgramRun(program_id=program.id, instance_id=inst.id,
                            org_id=org.id, kind="hook")
        db.add(queued)
        db.commit()
        qid = queued.id
    try:
        wp.run_program(qid)
        with SyncSession() as db:
            assert db.get(ProgramRun, qid).state == "queued"  # left for the sweep
    finally:
        with SyncSession() as db:
            db.execute(delete(ProgramRun).where(ProgramRun.instance_id == inst.id))
            db.execute(delete(ProgramInstance).where(ProgramInstance.id == inst.id))
            db.execute(delete(Program).where(Program.id == program.id))
            db.execute(delete(Organization).where(Organization.id == org.id))
            db.commit()


def test_dispatch_deferred_waits_for_free_instance():
    with SyncSession() as db:
        org = Organization(name="Hook Org", credit_balance=50.0)
        db.add(org)
        db.flush()
        program = Program(title="P", gitlab_repo_path=f"grp/hook-{next(_N)}",
                          is_published=True)
        db.add(program)
        db.flush()
        inst = ProgramInstance(program_id=program.id, org_id=org.id,
                               ssh_public_key="pk", ssh_private_key_enc="enc")
        db.add(inst)
        db.flush()
        running = ProgramRun(program_id=program.id, instance_id=inst.id,
                             org_id=org.id, kind="manual", state="running",
                             started_at=utcnow())
        db.add(running)
        old = ProgramRun(program_id=program.id, instance_id=inst.id,
                         org_id=org.id, kind="hook",
                         created_at=utcnow() - timedelta(minutes=2))
        db.add(old)
        db.flush()

        dispatch: list = []
        wp._dispatch_deferred(db, dispatch, utcnow())
        assert old.id not in dispatch  # instance busy

        running.state = "succeeded"
        db.flush()
        dispatch = []
        wp._dispatch_deferred(db, dispatch, utcnow())
        assert dispatch == [old.id]  # free now - oldest queued run goes

        fresh = ProgramRun(program_id=program.id, instance_id=inst.id,
                           org_id=org.id, kind="hook")
        db.add(fresh)
        db.flush()
        dispatch = []
        wp._dispatch_deferred(db, dispatch, utcnow())
        assert dispatch == [old.id]  # one per instance per sweep; fresh (<60s) waits
        db.rollback()
