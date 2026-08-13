"""Hub link tests: token-scope enforcement, credit-grant idempotency + atomic
balance, usage rollup, payment_due retrigger, and the no-op spoke tasks when no
hub is configured. HTTP paths run through the real app (TestClient) against the
dev DB and clean up every row they create (scoped to a throwaway org); the
worker no-op cases use monkeypatch only."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import new_api_token
from app.main import app
from app.models import (
    ApiToken, CreditTransaction, HubCreditGrant, Organization, Project, User,
)


@pytest.fixture(scope="module")
def client():
    # Entered as a context manager so every request shares one persistent event
    # loop; otherwise the module-level async engine pool caches asyncpg
    # connections across TestClient's per-request loops ("Event loop is closed").
    with TestClient(app) as c:
        yield c


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, user_id: str, scope: str) -> str:
    plaintext, token_hash = new_api_token()
    db.add(ApiToken(user_id=user_id, token_hash=token_hash, name=scope, scope=scope))
    return plaintext


@pytest.fixture
def hub_env():
    """Throwaway org + admin user with a user token and a hub token. Cleans up
    every row scoped to the org afterwards (transactions, grants, tokens,
    projects, user, org), so the shared dev DB is left untouched."""
    with SyncSession() as db:
        org = Organization(name="Hub Test Org", credit_balance=50.0)
        db.add(org)
        db.flush()
        user = User(org_id=org.id, email=f"hub-{uuid.uuid4().hex}@example.com",
                    password_hash="x", role="admin", email_verified=True)
        db.add(user)
        db.flush()
        env = {"org_id": org.id, "user_id": user.id,
               "user_token": _mint(db, user.id, "user"),
               "hub_token": _mint(db, user.id, "hub")}
        db.commit()
    try:
        yield env
    finally:
        oid, uid = env["org_id"], env["user_id"]
        with SyncSession() as db:
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(HubCreditGrant).where(HubCreditGrant.org_id == oid))
            db.execute(delete(ApiToken).where(ApiToken.user_id == uid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


# ---- scope enforcement ----

def test_user_token_forbidden_on_hub(client, hub_env):
    r = client.get("/api/hub/info", headers=_h(hub_env["user_token"]))
    assert r.status_code == 403


def test_hub_token_forbidden_on_knowledge(client, hub_env):
    # A valid body so the request reaches the auth dependency (not a 422).
    r = client.post("/api/knowledge/answer", headers=_h(hub_env["hub_token"]),
                    json={"query": "hello"})
    assert r.status_code == 403


def test_hub_token_reads_spoke_info(client, hub_env):
    r = client.get("/api/hub/info", headers=_h(hub_env["hub_token"]))
    assert r.status_code == 200
    body = r.json()
    assert "deploy_domain" in body and "credit_currency" in body
    assert body["org_count"] >= 1 and body["version"] == "0.1.0"
    # The hub reads capabilities + the billable speciality catalog over MCP
    # (spoke_info) rather than scraping the public settings page.
    assert body["capabilities"] == ["development"]
    assert body["specialities"], "enabled specialities must be published"
    for s in body["specialities"]:
        assert set(s) == {"id", "label", "description", "base_fee_credits"}
        assert isinstance(s["base_fee_credits"], (int, float)) and s["base_fee_credits"] >= 0


# ---- credit grants ----

def test_grant_happy_path(client, hub_env):
    oid = hub_env["org_id"]
    key = f"grant-{uuid.uuid4().hex}"
    r = client.post("/api/hub/credits/grant", headers=_h(hub_env["hub_token"]),
                    json={"org_id": oid, "amount": 25.0, "idempotency_key": key,
                          "detail": "welcome"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_applied"] is False
    assert body["credit_balance"] == pytest.approx(75.0)
    with SyncSession() as db:
        assert db.get(Organization, oid).credit_balance == pytest.approx(75.0)
        txns = db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == oid,
            CreditTransaction.kind == "hub_grant")).scalars().all()
        assert len(txns) == 1
        assert txns[0].amount == pytest.approx(25.0) and txns[0].project_id is None
        grants = db.execute(select(HubCreditGrant).where(
            HubCreditGrant.idempotency_key == key)).scalars().all()
        assert len(grants) == 1


def test_grant_org_id_from_query_param(client, hub_env):
    # The MCP sidecar forwards org_id as a QUERY param with it ABSENT from the body
    # (HUB_QUERY_KEYS). This is exactly the transfer_pump path; a body-only endpoint
    # 422'd it and stranded every brokered-org funding transfer. It must succeed.
    oid = hub_env["org_id"]
    with SyncSession() as db:
        before = db.get(Organization, oid).credit_balance or 0.0
    key = f"grant-{uuid.uuid4().hex}"
    r = client.post(f"/api/hub/credits/grant?org_id={oid}", headers=_h(hub_env["hub_token"]),
                    json={"amount": 15.0, "idempotency_key": key})
    assert r.status_code == 200, r.text
    assert r.json()["already_applied"] is False
    with SyncSession() as db:
        assert db.get(Organization, oid).credit_balance == pytest.approx(before + 15.0)


def test_grant_idempotent_replay(client, hub_env):
    oid = hub_env["org_id"]
    key = f"grant-{uuid.uuid4().hex}"
    payload = {"org_id": oid, "amount": 30.0, "idempotency_key": key}
    first = client.post("/api/hub/credits/grant", headers=_h(hub_env["hub_token"]), json=payload)
    second = client.post("/api/hub/credits/grant", headers=_h(hub_env["hub_token"]), json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["already_applied"] is False
    assert second.json()["already_applied"] is True
    with SyncSession() as db:
        # charged exactly once
        assert db.get(Organization, oid).credit_balance == pytest.approx(80.0)
        txns = db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == oid,
            CreditTransaction.kind == "hub_grant")).scalars().all()
        assert len(txns) == 1
        grants = db.execute(select(HubCreditGrant).where(
            HubCreditGrant.idempotency_key == key)).scalars().all()
        assert len(grants) == 1


@pytest.mark.parametrize("amount", [0, -5, 10001.0])
def test_grant_rejects_bad_amount(client, hub_env, amount):
    oid = hub_env["org_id"]
    r = client.post("/api/hub/credits/grant", headers=_h(hub_env["hub_token"]),
                    json={"org_id": oid, "amount": amount,
                          "idempotency_key": f"bad-{uuid.uuid4().hex}"})
    assert r.status_code == 400
    with SyncSession() as db:
        assert db.get(Organization, oid).credit_balance == pytest.approx(50.0)  # unchanged


def test_grant_unknown_org_404(client, hub_env):
    r = client.post("/api/hub/credits/grant", headers=_h(hub_env["hub_token"]),
                    json={"org_id": "does-not-exist", "amount": 5.0,
                          "idempotency_key": f"g-{uuid.uuid4().hex}"})
    assert r.status_code == 404


# ---- usage rollup ----

def test_usage_rollup_math(client, hub_env):
    oid = hub_env["org_id"]
    hub_h = _h(hub_env["hub_token"])
    # A grant first: usage is scoped to hub-relevant orgs, and grant history is
    # what makes this org visible to the hub at all.
    r = client.post("/api/hub/credits/grant", headers=hub_h,
                    json={"org_id": oid, "amount": 1.0,
                          "idempotency_key": f"vis-{uuid.uuid4().hex}"})
    assert r.status_code == 200
    before = client.get("/api/hub/usage", headers=hub_h).json()["by_kind"]
    with SyncSession() as db:
        db.add(CreditTransaction(org_id=oid, amount=100.0, kind="topup"))
        db.add(CreditTransaction(org_id=oid, amount=-12.0, kind="consumption"))
        db.add(CreditTransaction(org_id=oid, amount=-3.0, kind="consumption"))
        db.commit()
    after = client.get("/api/hub/usage", headers=hub_h).json()
    assert round(after["by_kind"].get("topup", 0) - before.get("topup", 0), 4) == 100.0
    assert round(after["by_kind"].get("consumption", 0) - before.get("consumption", 0), 4) == -15.0
    assert after["cursor"] is not None


# ---- privacy boundary: per-org data is scoped to hub-relevant orgs ----

def test_usage_and_events_invisible_for_direct_org(client, hub_env):
    """An org the hub never created nor funded (= the spoke owner's direct
    business) must not appear in /usage or /credit-events."""
    from datetime import datetime, timedelta, timezone
    oid = hub_env["org_id"]  # no grant applied in this test -> direct org
    hub_h = _h(hub_env["hub_token"])
    # page from just before this test so the 500-row cap can't hide our rows
    since = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    before = client.get("/api/hub/usage", headers=hub_h).json()["by_kind"]
    with SyncSession() as db:
        txn = CreditTransaction(org_id=oid, amount=-42.0, kind="consumption",
                                detail="direct customer secret project")
        db.add(txn)
        db.commit()
        tid = txn.id
    after = client.get("/api/hub/usage", headers=hub_h).json()["by_kind"]
    assert round(after.get("consumption", 0) - before.get("consumption", 0), 4) == 0.0
    events = client.get("/api/hub/credit-events", params={"since": since},
                        headers=hub_h).json()["events"]
    assert tid not in {e["id"] for e in events}
    # funding the org flips it hub-relevant: the same rows become visible
    r = client.post("/api/hub/credits/grant", headers=hub_h,
                    json={"org_id": oid, "amount": 1.0,
                          "idempotency_key": f"flip-{uuid.uuid4().hex}"})
    assert r.status_code == 200
    events = client.get("/api/hub/credit-events", params={"since": since},
                        headers=hub_h).json()["events"]
    assert tid in {e["id"] for e in events}


# ---- brokered org creation ----

def test_create_org_idempotent(client, hub_env):
    hub_h = _h(hub_env["hub_token"])
    key = f"org-{uuid.uuid4().hex}"
    created: list[str] = []
    try:
        first = client.post("/api/hub/orgs", headers=hub_h,
                            json={"name": "Hub customer 7f3a", "idempotency_key": key})
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["already_created"] is False and body["credit_balance"] == 0.0
        created.append(body["org_id"])
        replay = client.post("/api/hub/orgs", headers=hub_h,
                             json={"name": "Hub customer 7f3a", "idempotency_key": key})
        assert replay.status_code == 201
        assert replay.json()["already_created"] is True
        assert replay.json()["org_id"] == body["org_id"]
        with SyncSession() as db:
            org = db.get(Organization, body["org_id"])
            assert org.hub_managed is True and org.hub_create_key == key
            # brokered = userless: nothing to log in with, nothing to email
            assert db.execute(select(User).where(
                User.org_id == org.id)).scalars().all() == []
    finally:
        with SyncSession() as db:
            for oid in created:
                db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
                db.execute(delete(HubCreditGrant).where(HubCreditGrant.org_id == oid))
                db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def test_create_org_user_token_forbidden(client, hub_env):
    r = client.post("/api/hub/orgs", headers=_h(hub_env["user_token"]),
                    json={"name": "X", "idempotency_key": f"k-{uuid.uuid4().hex}"})
    assert r.status_code == 403


# ---- stolen-token backstops ----

def test_daily_grant_ceiling(client, hub_env, monkeypatch):
    from app.core.config import settings as cfg
    oid = hub_env["org_id"]
    hub_h = _h(hub_env["hub_token"])
    # ceiling counts ALL grants in 24h (shared dev DB may hold some) - set it
    # relative to the current day total so the test is hermetic
    with SyncSession() as db:
        from sqlalchemy import func as f
        from datetime import timedelta
        from app.models import utcnow
        day_total = db.execute(select(f.coalesce(f.sum(HubCreditGrant.amount), 0.0))
                               .where(HubCreditGrant.created_at
                                      > utcnow() - timedelta(hours=24))).scalar_one()
    monkeypatch.setattr(cfg, "hub_max_daily_grant_credits", day_total + 50.0)
    ok = client.post("/api/hub/credits/grant", headers=hub_h,
                     json={"org_id": oid, "amount": 40.0,
                           "idempotency_key": f"cap-{uuid.uuid4().hex}"})
    assert ok.status_code == 200
    over = client.post("/api/hub/credits/grant", headers=hub_h,
                       json={"org_id": oid, "amount": 20.0,
                             "idempotency_key": f"cap-{uuid.uuid4().hex}"})
    assert over.status_code == 429
    with SyncSession() as db:  # ceiling refused before any write
        assert db.get(Organization, oid).credit_balance == pytest.approx(90.0)


def test_find_org_rate_limited(client, hub_env):
    hub_h = _h(hub_env["hub_token"])
    codes = [client.get("/api/hub/orgs/find",
                        params={"email": f"nobody-{i}@example.com"},
                        headers=hub_h).status_code for i in range(31)]
    assert set(codes[:30]) == {404}
    assert codes[30] == 429


# ---- outbound usage push is scoped the same way ----

def test_usage_report_scoped_to_hub_orgs(hub_env, monkeypatch):
    from datetime import datetime, timezone
    from app.workers import hub as hub_worker

    oid = hub_env["org_id"]
    start = datetime.now(timezone.utc)
    with SyncSession() as db:
        direct = CreditTransaction(org_id=oid, amount=-5.0, kind="consumption",
                                   detail="direct - must not leave the spoke")
        db.add(direct)
        db.commit()
        direct_id = direct.id
        # a second org, hub-managed, with one txn
        hub_org = Organization(name="Hub customer test", hub_managed=True,
                               hub_create_key=f"k-{uuid.uuid4().hex}")
        db.add(hub_org)
        db.flush()
        hubtxn = CreditTransaction(org_id=hub_org.id, amount=-7.0, kind="consumption")
        db.add(hubtxn)
        db.commit()
        hub_org_id, hub_txn_id = hub_org.id, hubtxn.id
    sent: list = []
    try:
        monkeypatch.setattr(hub_worker.settings, "hub_mcp_url", "http://hub.test/mcp")
        monkeypatch.setattr(hub_worker.app_settings, "get_setting_sync",
                            lambda db, key, default=None: start.isoformat())
        monkeypatch.setattr(hub_worker.app_settings, "set_setting_sync",
                            lambda db, key, value: None)  # never move the real cursor
        monkeypatch.setattr(hub_worker.hub_client, "report_usage",
                            lambda events: sent.append(events) or {"acked": len(events)})
        hub_worker.hub_usage_report()
        ids = {e["ext_id"] for batch in sent for e in batch}
        assert hub_txn_id in ids
        assert direct_id not in ids
    finally:
        with SyncSession() as db:
            db.execute(delete(CreditTransaction).where(
                CreditTransaction.org_id == hub_org_id))
            db.execute(delete(Organization).where(Organization.id == hub_org_id))
            db.commit()


# ---- payment_due retrigger ----

def test_grant_retriggers_payment_due(client, hub_env, monkeypatch):
    oid = hub_env["org_id"]
    sent: list = []
    from app.api import hub as hub_api
    monkeypatch.setattr(hub_api.celery, "send_task",
                        lambda name, args=None, **k: sent.append((name, args)))
    with SyncSession() as db:
        proj = Project(org_id=oid, name="P", description="d", status="payment_due")
        db.add(proj)
        db.commit()
        pid = proj.id
    r = client.post("/api/hub/credits/grant", headers=_h(hub_env["hub_token"]),
                    json={"org_id": oid, "amount": 40.0,
                          "idempotency_key": f"g-{uuid.uuid4().hex}"})
    assert r.status_code == 200
    assert ("app.workers.tasks.maybe_start_development", [pid]) in sent


# ---- spoke tasks are no-ops without a hub ----

def test_hub_tasks_noop_without_hub(monkeypatch):
    from app.workers import hub as hub_worker

    monkeypatch.setattr(hub_worker.settings, "hub_mcp_url", "")

    def _fail(*a, **k):
        pytest.fail("hub_client must not be called when no hub is configured")

    monkeypatch.setattr(hub_worker.hub_client, "register_spoke", _fail)
    monkeypatch.setattr(hub_worker.hub_client, "heartbeat", _fail)
    monkeypatch.setattr(hub_worker.hub_client, "report_usage", _fail)
    assert hub_worker.hub_heartbeat() is None
    assert hub_worker.hub_usage_report() is None
