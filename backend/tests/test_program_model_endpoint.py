"""Program model config via saved ModelEndpoint (§28): resolve precedence
(instance pick > program endpoint > legacy inline trio > global settings), the
leak-scan key helper, the endpoint-price billing fallback in record_org_usage,
the admin PUT semantics (picking a selection clears the legacy trio; validation
mirrors the per-project model-config route), the customer-side per-instance
picker, and the endpoint DELETE guard.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.core.security import hash_password
from app.main import app
from app.models import (
    CreditTransaction, ModelEndpoint, Organization, Program, ProgramInstance, User,
)
from app.services import events, llm
from app.services import programs as programs_svc
from app.services.pricing import cost_credits

_N = iter(range(10000))


def _endpoint(db, **kw):
    kw.setdefault("label", f"EP-{next(_N)}")
    kw.setdefault("provider", "custom")
    kw.setdefault("base_url", "https://models.example/v1")
    kw.setdefault("api_key_enc", encrypt("ep-key-123"))
    kw.setdefault("model_name", "mistral-large-latest")
    ep = ModelEndpoint(**kw)
    db.add(ep)
    db.flush()
    return ep


def _program(db, **kw):
    kw.setdefault("title", "Prog")
    kw.setdefault("gitlab_repo_path", f"grp/mep-{next(_N)}")
    p = Program(**kw)
    db.add(p)
    db.flush()
    return p


def _org(db, name="MEP Org"):
    org = Organization(name=name)
    db.add(org)
    db.flush()
    return org


def _instance(db, program, org_id=None, **kw):
    inst = ProgramInstance(program_id=program.id,
                           org_id=org_id or _org(db).id,
                           ssh_public_key="pk", ssh_private_key_enc=encrypt("sk"), **kw)
    db.add(inst)
    db.flush()
    return inst


# ---------------------------------------------------------------- resolution

def test_resolve_prefers_endpoint_then_legacy_then_global():
    with SyncSession() as db:
        try:
            ep = _endpoint(db, base_url="https://ep.example/v1", model_name="ep-model")
            program = _program(db, model_endpoint_id=ep.id,
                               openai_base_url="https://legacy.example/v1",
                               openai_api_key_enc=encrypt("legacy-key"),
                               model_name="legacy-model")
            assert programs_svc.resolve_model_config(db, program) == (
                "https://ep.example/v1", "ep-key-123", "ep-model")

            program.model_endpoint_id = None
            assert programs_svc.resolve_model_config(db, program) == (
                "https://legacy.example/v1", "legacy-key", "legacy-model")

            program.openai_base_url = None
            program.openai_api_key_enc = None
            program.model_name = None
            assert programs_svc.resolve_model_config(db, program) == (
                settings.openai_base_url, settings.openai_api_key, settings.openai_model)

            # An endpoint with no model degrades to the fallbacks, never a half-config.
            bare = _endpoint(db, model_name=None)
            program.model_endpoint_id = bare.id
            assert programs_svc.resolve_model_config(db, program)[2] == settings.openai_model
        finally:
            db.rollback()


def test_resolve_prefers_the_instance_pick_over_the_program_default():
    """§28 per-instance model: the customer's pick wins, absence inherits the
    program default, and an instance pointed at a model-less endpoint degrades
    down the same chain instead of half-configuring the sandbox."""
    with SyncSession() as db:
        try:
            prog_ep = _endpoint(db, base_url="https://prog.example/v1",
                                api_key_enc=encrypt("prog-key"), model_name="prog-model")
            inst_ep = _endpoint(db, base_url="https://inst.example/v1",
                                api_key_enc=encrypt("inst-key"), model_name="inst-model")
            program = _program(db, model_endpoint_id=prog_ep.id)
            inst = _instance(db, program, model_endpoint_id=inst_ep.id)

            assert programs_svc.resolve_model_config(db, program, inst) == (
                "https://inst.example/v1", "inst-key", "inst-model")
            # no pick, and the check-run path (instance=None), both inherit
            inst.model_endpoint_id = None
            assert programs_svc.resolve_model_config(db, program, inst) == (
                "https://prog.example/v1", "prog-key", "prog-model")
            assert programs_svc.resolve_model_config(db, program) == (
                "https://prog.example/v1", "prog-key", "prog-model")

            bare = _endpoint(db, model_name=None)
            inst.model_endpoint_id = bare.id
            assert programs_svc.resolve_model_config(db, program, inst) == (
                "https://prog.example/v1", "prog-key", "prog-model")
        finally:
            db.rollback()


def test_program_model_keys_cover_every_resolvable_credential():
    """The refuse set is a superset on purpose: both endpoints in the resolution
    chain plus the legacy inline key, so no key that could reach the sandbox is
    missing from the leak scan."""
    with SyncSession() as db:
        try:
            prog_ep = _endpoint(db, api_key_enc=encrypt("prog-key"))
            inst_ep = _endpoint(db, api_key_enc=encrypt("inst-key"))
            program = _program(db, openai_api_key_enc=encrypt("legacy-key"))
            assert programs_svc.program_model_keys(program, [inst_ep, prog_ep]) == [
                "inst-key", "prog-key", "legacy-key"]
            assert programs_svc.program_model_keys(program, []) == ["legacy-key"]
            program.openai_api_key_enc = None
            assert programs_svc.program_model_keys(program, []) == []
        finally:
            db.rollback()


def test_write_program_env_uses_the_instance_model(tmp_path):
    """The sandbox env is resolved for THIS instance, not the program default."""
    with SyncSession() as db:
        try:
            ep = _endpoint(db, base_url="https://inst.example/v1",
                           api_key_enc=encrypt("inst-key"), model_name="inst-model")
            program = _program(db, cpu_limit="1", mem_limit="1g", mem_request="256m")
            inst = _instance(db, program, model_endpoint_id=ep.id)
            programs_svc.write_program_env(db, tmp_path, program, inst)
            env = (tmp_path / ".openvisor" / "program.env").read_text()
            assert "OPENAI_MODEL='inst-model'" in env
            assert "OPENAI_API_KEY='inst-key'" in env
        finally:
            db.rollback()


# ---------------------------------------------------------------- billing

def test_record_org_usage_endpoint_price_fallback():
    """A program routed to a model NOT in the static price table bills through
    the endpoint's admin-supplied per-1M prices (record_usage parity) instead of
    raising UnknownModelError."""
    model = f"custom-bill-{next(_N)}"
    with SyncSession() as db:
        try:
            org = Organization(name="MEP Org", credit_balance=100.0)
            db.add(org)
            db.flush()
            _endpoint(db, model_name=model, input_price=3.0, output_price=9.0)
            usage = {"model": model, "input_tokens": 1_000_000, "output_tokens": 100_000}
            charged = llm.record_org_usage(db, org.id, [usage], "test", kind="program_run",
                                           markup=2.0)
            assert charged == pytest.approx(
                cost_credits(model, 1_000_000, 100_000, markup=2.0, price=(3.0, 9.0)))
            assert org.credit_balance == pytest.approx(100.0 - charged)
        finally:
            db.rollback()


# ---------------------------------------------------------------- HTTP admin

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _login(client, role):
    """Create an org + user of `role`, log the shared client in as them, and
    yield the CSRF headers. One logged-in session per test - the fixtures below
    are mutually exclusive."""
    email = f"mep-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "mep-secret-1234"
    with SyncSession() as db:
        org = Organization(name=f"MEP HTTP Org ({role})")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role=role, email_verified=True))
        db.commit()
        oid = org.id
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    try:
        yield {"X-CSRF-Token": tok}
    finally:
        with SyncSession() as db:
            db.execute(delete(ProgramInstance).where(ProgramInstance.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


@pytest.fixture
def admin(client):
    yield from _login(client, "admin")


@pytest.fixture
def customer(client):
    yield from _login(client, "customer")


def test_admin_put_endpoint_semantics(client, admin):
    with SyncSession() as db:
        ep = _endpoint(db)
        bare = _endpoint(db, model_name=None)
        program = _program(db, openai_base_url="https://legacy.example/v1",
                           openai_api_key_enc=encrypt("legacy-key"),
                           model_name="legacy-model")
        db.commit()
        pid, epid, bareid = program.id, ep.id, bare.id
    try:
        r = client.get(f"/api/admin/programs/{pid}", headers=admin)
        assert r.status_code == 200 and r.json()["has_legacy_model_config"] is True

        # unknown endpoint -> 404; endpoint without a model -> 400
        assert client.put(f"/api/admin/programs/{pid}",
                          json={"model_endpoint_id": "no-such"}, headers=admin
                          ).status_code == 404
        assert client.put(f"/api/admin/programs/{pid}",
                          json={"model_endpoint_id": bareid}, headers=admin
                          ).status_code == 400

        # picking an endpoint clears the legacy trio
        r = client.put(f"/api/admin/programs/{pid}",
                       json={"model_endpoint_id": epid}, headers=admin)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["model_endpoint_id"] == epid
        assert body["has_legacy_model_config"] is False
        with SyncSession() as db:
            row = db.get(Program, pid)
            assert row.openai_base_url is None and row.openai_api_key_enc is None
            assert row.model_name is None

        # the endpoint can't be deleted while referenced
        r = client.delete(f"/api/admin/model-endpoints/{epid}", headers=admin)
        assert r.status_code == 409 and "program" in r.json()["detail"].lower()

        # omitting the field keeps the selection; null resets to global default
        r = client.put(f"/api/admin/programs/{pid}", json={"title": "Prog2"}, headers=admin)
        assert r.json()["model_endpoint_id"] == epid
        r = client.put(f"/api/admin/programs/{pid}",
                       json={"model_endpoint_id": None}, headers=admin)
        assert r.status_code == 200 and r.json()["model_endpoint_id"] is None
    finally:
        with SyncSession() as db:
            db.execute(delete(Program).where(Program.id == pid))
            db.execute(delete(ModelEndpoint).where(ModelEndpoint.id.in_((epid, bareid))))
            db.commit()


def test_customer_pins_and_clears_the_instance_model(client, customer):
    """§28 per-instance model, customer path: the selectable list exposes no
    secret, the pick round-trips, "" clears it back to the program default, and
    an unselectable endpoint (unknown, or saved without a model) 404s."""
    with SyncSession() as db:
        ep = _endpoint(db, label="Fast + cheap", model_name="pick-me-model")
        bare = _endpoint(db, model_name=None)
        program = _program(db, is_published=True)
        db.commit()
        pid, epid, bareid = program.id, ep.id, bare.id
    iid = None
    try:
        options = client.get("/api/program-model-endpoints", headers=customer).json()
        chosen = next(o for o in options if o["id"] == epid)
        assert chosen == {"id": epid, "label": "Fast + cheap",
                          "model_name": "pick-me-model"}
        # the model-less endpoint is not offered: nothing could resolve through it
        assert not any(o["id"] == bareid for o in options)

        r = client.post(f"/api/programs/{pid}/instances", json={"label": "mine"},
                        headers=customer)
        assert r.status_code == 200, r.text
        iid = r.json()["id"]
        assert r.json()["model_endpoint_id"] is None  # inherits the program default

        r = client.put(f"/api/program-instances/{iid}",
                       json={"model_endpoint_id": epid}, headers=customer)
        assert r.status_code == 200 and r.json()["model_endpoint_id"] == epid

        # an admin can no longer delete the endpoint out from under the instance
        with SyncSession() as db:
            assert db.get(ProgramInstance, iid).model_endpoint_id == epid

        for bad in ("no-such-endpoint", bareid):
            assert client.put(f"/api/program-instances/{iid}",
                              json={"model_endpoint_id": bad}, headers=customer
                              ).status_code == 404
        # ...and the failed attempts left the pick alone
        assert client.get(f"/api/program-instances/{iid}",
                          headers=customer).json()["model_endpoint_id"] == epid

        r = client.put(f"/api/program-instances/{iid}",
                       json={"model_endpoint_id": ""}, headers=customer)
        assert r.status_code == 200 and r.json()["model_endpoint_id"] is None
    finally:
        with SyncSession() as db:
            if iid:
                db.execute(delete(ProgramInstance).where(ProgramInstance.id == iid))
            db.execute(delete(Program).where(Program.id == pid))
            db.execute(delete(ModelEndpoint).where(ModelEndpoint.id.in_((epid, bareid))))
            db.commit()


def test_endpoint_delete_guard_counts_program_instances(client, admin):
    """An instance's pick is a reference the program row doesn't show - deleting
    the endpoint anyway would silently drop the customer back to another model."""
    with SyncSession() as db:
        ep = _endpoint(db)
        program = _program(db)
        inst = _instance(db, program, model_endpoint_id=ep.id)
        db.commit()
        pid, epid, iid, oid = program.id, ep.id, inst.id, inst.org_id
    try:
        r = client.delete(f"/api/admin/model-endpoints/{epid}", headers=admin)
        assert r.status_code == 409
        assert "program instance" in r.json()["detail"].lower()
    finally:
        with SyncSession() as db:
            db.execute(delete(ProgramInstance).where(ProgramInstance.id == iid))
            db.execute(delete(Program).where(Program.id == pid))
            db.execute(delete(ModelEndpoint).where(ModelEndpoint.id == epid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()
