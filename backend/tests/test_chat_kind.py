"""§chat kind - "Just chat with me": creation rules (opt-in pause flag, upfront
fee in the create transaction, born in `development`, no sandbox scaffolding,
description seeds the thread), the answer task (KB-grounded, Memory-aware,
verbatim-guarded, billed as project consumption, dedup on Message.meta.answers,
wallet/rate guards), and the dispatch split (chat -> responder, never the §12
classifier)."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.core.security import hash_password, new_api_token
from app.main import app
from app.models import (
    ApiToken, AppSetting, CreditTransaction, HubProjectEvent, KnowledgeChunk,
    Message, Organization, Project, ProjectMemory, StatusChange, User,
)
from app.services import events
from app.workers import tasks


class FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def set(self, key, val, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, ttl):
        return True

    def publish(self, channel, payload):
        return 0


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(events, "get_sync_redis", lambda: fake)
    return fake


@pytest.fixture
def chat_unpaused():
    """Chat deposits are opt-in (paused by default) - enable them for the test."""
    with SyncSession() as db:
        row = db.get(AppSetting, "pause_chat_deposits")
        if row is None:
            db.add(AppSetting(key="pause_chat_deposits", value=False))
        else:
            row.value = False
        db.commit()
    yield
    with SyncSession() as db:
        db.execute(delete(AppSetting).where(AppSetting.key == "pause_chat_deposits"))
        db.commit()


@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="Chat Org", credit_balance=100.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(HubProjectEvent).where(HubProjectEvent.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(ProjectMemory).where(ProjectMemory.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _chat_project(oid, **kw):
    kw.setdefault("name", "Chat")
    kw.setdefault("description", "Let's talk sovereignty.")
    kw.setdefault("kind", "chat")
    kw.setdefault("status", "development")
    kw.setdefault("block_auto_development", True)
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.commit()
        return p.id


def _msg(pid, body="How do I isolate workloads?", author="customer"):
    with SyncSession() as db:
        m = Message(project_id=pid, thread="main", author=author, body=body)
        db.add(m)
        db.commit()
        return m.id


def _chunk(content, file="local/doc.md"):
    return KnowledgeChunk(source="kb", path=f"{file}#0", content=content,
                          meta={"file": file})


EMB = {"model": "mistral-embed", "input_tokens": 50, "output_tokens": 0}
SYN = {"model": "mistral-large-latest", "input_tokens": 300, "output_tokens": 80}


def _wire_llm(monkeypatch, answer="Isolation works via namespaces [1].",
              chunks=None):
    chunks = [_chunk("Sovereign isolation relies on hard multi-tenancy boundaries.")] \
        if chunks is None else chunks
    monkeypatch.setattr("app.services.pricing.is_priced", lambda m: True)
    monkeypatch.setattr(tasks.rag, "retrieve",
                        lambda db, q, k=6, kb_ids=None: (chunks, [dict(EMB)]))
    monkeypatch.setattr(tasks.llm, "chat",
                        lambda messages, **kw: (answer, dict(SYN)))
    monkeypatch.setattr(tasks.emailer, "send_email", lambda *a, **k: True)


# ---------------------------------------------------------------- answer task

def test_answer_posts_bills_and_dedups(org, fake_redis, monkeypatch):
    pid = _chat_project(org)
    mid = _msg(pid)
    _wire_llm(monkeypatch)

    tasks.answer_chat_message(pid, mid)
    with SyncSession() as db:
        agent = db.query(Message).filter_by(project_id=pid, author="agent").all()
        assert len(agent) == 1
        assert agent[0].meta == {"answers": mid}
        assert "Isolation works" in agent[0].body
        assert "Sources: [1] local/doc.md" in agent[0].body
        p = db.get(Project, pid)
        assert p.tokens_consumed == 430  # 50 + 300 + 80 across both calls
        assert p.cost_credits > 0
        o = db.get(Organization, org)
        assert o.credit_balance < 100.0
        txns = db.execute(select(CreditTransaction).where(
            CreditTransaction.org_id == org)).scalars().all()
        assert [t.kind for t in txns] == ["consumption"]
        assert txns[0].project_id == pid

    # A second dispatch for the same message is a no-op (meta.answers dedup).
    tasks.answer_chat_message(pid, mid)
    with SyncSession() as db:
        assert db.query(Message).filter_by(project_id=pid, author="agent").count() == 1


def test_answer_only_in_development_and_backs_off_for_admin(org, fake_redis, monkeypatch):
    _wire_llm(monkeypatch)
    parked = _chat_project(org, status="awaiting_admin")
    tasks.answer_chat_message(parked, _msg(parked))
    with SyncSession() as db:
        assert db.query(Message).filter_by(project_id=parked, author="agent").count() == 0

    active = _chat_project(org)
    _msg(active)
    admin_last = _msg(active, body="Consultant here, I'll take this.", author="admin")
    tasks.answer_chat_message(active, admin_last)
    with SyncSession() as db:
        assert db.query(Message).filter_by(project_id=active, author="agent").count() == 0


def test_answer_admin_agent_mention_is_answered(org, fake_redis, monkeypatch):
    """An admin message that summons the agent (@agent/@ai) is answered like a
    customer question - a mention is never met with silence - while a plain
    admin follow-up afterwards still hands the thread back to the human."""
    _wire_llm(monkeypatch)
    pid = _chat_project(org)
    summons = _msg(pid, body="@agent What are examples of sovereignty incidents?",
                   author="admin")
    tasks.answer_chat_message(pid, summons)
    with SyncSession() as db:
        agent = db.query(Message).filter_by(project_id=pid, author="agent").all()
        assert len(agent) == 1
        assert agent[0].meta == {"answers": summons}

    plain = _msg(pid, body="Thanks, I'll take it from here.", author="admin")
    tasks.answer_chat_message(pid, plain)
    with SyncSession() as db:
        assert db.query(Message).filter_by(project_id=pid, author="agent").count() == 1


def test_sources_credit_only_what_the_answer_cited(org, fake_redis, monkeypatch):
    """Six chunks out of one README used to print six identical lines. The answer
    cites two of them, so the trailer names one file and carries both markers."""
    pid = _chat_project(org)
    mid = _msg(pid)
    kb = "cdc7e227-d31a-4d93-8142-b25f704e72b5"
    _wire_llm(monkeypatch,
              answer="Buy the book [2] or sponsor the author [4].",
              chunks=[_chunk(f"part {i}", file=f"{kb}/README.md") for i in range(6)])

    tasks.answer_chat_message(pid, mid)
    with SyncSession() as db:
        body = db.query(Message).filter_by(project_id=pid, author="agent").one().body
    assert "Sources: [2][4] README.md" in body
    # the repetition is gone, and so is the identifier that meant nothing to a reader
    assert body.count("README.md") == 1
    assert kb not in body


def test_sources_keep_distinct_files_apart(org, fake_redis, monkeypatch):
    """Grouping must not merge two different files, and the markers stay attached to
    the positions the model actually wrote into its prose."""
    pid = _chat_project(org)
    mid = _msg(pid)
    _wire_llm(monkeypatch,
              answer="Setup is here [1], and the licence is here [3].",
              chunks=[_chunk("a", file="README.md"), _chunk("b", file="README.md"),
                      _chunk("c", file="LICENCE.md")])

    tasks.answer_chat_message(pid, mid)
    with SyncSession() as db:
        body = db.query(Message).filter_by(project_id=pid, author="agent").one().body
    assert "Sources: [1] README.md · [3] LICENCE.md" in body


def test_an_answer_that_cites_nothing_gets_no_sources_trailer(org, fake_redis, monkeypatch):
    """Chunks were retrieved but the model leaned on none of them: crediting sources
    it did not use would put words in the answer's mouth."""
    pid = _chat_project(org)
    mid = _msg(pid)
    _wire_llm(monkeypatch, answer="I do not have anything on that yet.",
              chunks=[_chunk("unused", file="README.md")])

    tasks.answer_chat_message(pid, mid)
    with SyncSession() as db:
        body = db.query(Message).filter_by(project_id=pid, author="agent").one().body
    assert "Sources:" not in body


def test_answer_verbatim_guard_redacts(org, fake_redis, monkeypatch):
    secret = ("alpha bravo charlie delta echo foxtrot golf hotel india juliett "
              "kilo lima mike november oscar")
    pid = _chat_project(org)
    mid = _msg(pid)
    _wire_llm(monkeypatch, answer=f"As documented: {secret} - hope that helps [1].",
              chunks=[_chunk(f"The playbook lists {secret} in order.")])
    tasks.answer_chat_message(pid, mid)
    with SyncSession() as db:
        agent = db.query(Message).filter_by(project_id=pid, author="agent").one()
        assert "[…]" in agent.body
        assert secret not in agent.body


def test_answer_empty_wallet_notices_once(org, fake_redis, monkeypatch):
    _wire_llm(monkeypatch)
    monkeypatch.setattr(tasks.rag, "retrieve",
                        lambda *a, **k: pytest.fail("must not retrieve when broke"))
    pid = _chat_project(org)
    with SyncSession() as db:
        db.get(Organization, org).credit_balance = 0.0
        db.commit()
    tasks.answer_chat_message(pid, _msg(pid))
    tasks.answer_chat_message(pid, _msg(pid, body="hello?"))
    with SyncSession() as db:
        notices = db.query(Message).filter_by(project_id=pid, author="agent").all()
        assert len(notices) == 1
        assert "balance is empty" in notices[0].body


def test_answer_rate_cap(org, fake_redis, monkeypatch):
    _wire_llm(monkeypatch)
    monkeypatch.setattr(tasks.settings, "chat_answer_rate_per_10min", 0)
    pid = _chat_project(org)
    tasks.answer_chat_message(pid, _msg(pid))
    with SyncSession() as db:
        agent = db.query(Message).filter_by(project_id=pid, author="agent").all()
        assert len(agent) == 1
        assert "faster than I can answer" in agent[0].body


def test_answer_lock_held_retries(org, fake_redis, monkeypatch):
    from celery.exceptions import Retry
    _wire_llm(monkeypatch)
    pid = _chat_project(org)
    mid = _msg(pid)
    fake_redis.set(f"chatans:{pid}", "someone-else")
    with pytest.raises(Retry):
        tasks.answer_chat_message(pid, mid)


def test_memory_block_withholds_secrets(org):
    pid = _chat_project(org)
    with SyncSession() as db:
        db.add(ProjectMemory(project_id=pid, author="customer", key="STACK",
                             value_enc=encrypt("FastAPI"), is_secret=False,
                             description="preferred stack"))
        db.add(ProjectMemory(project_id=pid, author="customer", key="API_KEY",
                             value_enc=encrypt("sk-verysecret"), is_secret=True))
        db.commit()
        p = db.get(Project, pid)
        block = tasks._chat_memory_block(db, p)
    assert "STACK = FastAPI" in block
    assert "API_KEY (secret, value withheld)" in block
    assert "sk-verysecret" not in block


# ---------------------------------------------------------------- HTTP creation

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
        # the chat routes publish WS events, binding the shared async redis
        # client to THIS TestClient's loop - drop it (and the engine's pooled
        # connections) so later test modules start on their own loop
        asyncio.run(engine.dispose(close=False))
        events._async_client = None


def _login(client, balance=100.0, role="customer"):
    email = f"chat-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "chat-secret-12"
    with SyncSession() as db:
        org = Organization(name="Chat HTTP Org", credit_balance=balance)
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
    return {"X-CSRF-Token": tok}, oid


def _cleanup_org(oid):
    with SyncSession() as db:
        pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
        if pids:
            db.execute(delete(Message).where(Message.project_id.in_(pids)))
            db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
        db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
        db.execute(delete(Project).where(Project.org_id == oid))
        db.execute(delete(User).where(User.org_id == oid))
        db.execute(delete(Organization).where(Organization.id == oid))
        db.commit()


def test_create_chat_project(client, chat_unpaused, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    sent: list = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: sent.append(a))
    h, oid = _login(client)
    try:
        r = client.post("/api/projects", json={
            "kind": "chat", "description": "Talk to me about sovereign clouds.",
            "from_scratch": True, "sovereign": False}, headers=h)
        assert r.status_code == 201, r.text
        p = r.json()
        assert p["kind"] == "chat"
        assert p["status"] == "development"  # live immediately, no evaluation
        assert p["ssh_public_key"] is None and p["subdomain"] is None  # no scaffolding
        with SyncSession() as db:
            o = db.get(Organization, oid)
            assert o.credit_balance == pytest.approx(90.0)  # 10-credit opening fee
            txn = db.execute(select(CreditTransaction).where(
                CreditTransaction.org_id == oid)).scalars().one()
            assert txn.kind == "chat_upfront" and txn.amount == pytest.approx(-10.0)
            seed = db.query(Message).filter_by(project_id=p["id"], thread="main").one()
            assert seed.author == "customer"
            assert seed.body == "Talk to me about sovereign clouds."
        # the seed dispatched the responder, never the §12 classifier / provisioning
        names = [a[0] for a in sent]
        assert "app.workers.tasks.answer_chat_message" in names
        assert "app.workers.tasks.classify_chat_message" not in names
        assert "app.workers.tasks.provision_project" not in names

        # repos are rejected on chat projects
        r = client.post("/api/projects", json={
            "kind": "chat", "description": "with repo?", "from_scratch": False,
            "sovereign": False, "repos": [{"ssh_uri": "git@github.com:a/b.git"}]},
            headers=h)
        assert r.status_code == 400
    finally:
        _cleanup_org(oid)


def test_create_chat_needs_balance(client, chat_unpaused, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    h, oid = _login(client, balance=5.0)
    try:
        r = client.post("/api/projects", json={
            "kind": "chat", "description": "broke", "from_scratch": True,
            "sovereign": False}, headers=h)
        assert r.status_code == 402
        with SyncSession() as db:
            assert db.execute(select(Project).where(
                Project.org_id == oid)).scalars().all() == []
            assert db.get(Organization, oid).credit_balance == pytest.approx(5.0)
    finally:
        _cleanup_org(oid)


def test_create_chat_paused_by_default(client, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    h, oid = _login(client)
    try:
        r = client.post("/api/projects", json={
            "kind": "chat", "description": "opt-in?", "from_scratch": True,
            "sovereign": False}, headers=h)
        assert r.status_code == 403 and r.json()["detail"] == "deposits_paused"
    finally:
        _cleanup_org(oid)


def test_chat_message_routes_to_responder_not_classifier(client, chat_unpaused,
                                                         monkeypatch):
    from app.workers.celery_app import celery as celery_app
    sent: list = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: sent.append(a))
    h, oid = _login(client)
    try:
        r = client.post("/api/projects", json={
            "kind": "chat", "description": "hello", "from_scratch": True,
            "sovereign": False}, headers=h)
        pid = r.json()["id"]
        sent.clear()
        r = client.post(f"/api/projects/{pid}/messages",
                        json={"thread": "main", "body": "and another question"},
                        headers=h)
        assert r.status_code == 201, r.text
        names = [a[0] for a in sent]
        assert names == ["app.workers.tasks.answer_chat_message"]

        # requests are a build-pipeline surface - chat projects refuse them
        r = client.post(f"/api/projects/{pid}/requests",
                        json={"type": "feature", "handling": "ai", "body": "build me X"},
                        headers=h)
        assert r.status_code == 409
    finally:
        _cleanup_org(oid)


def test_admin_chat_message_dispatches_only_on_mention(client, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    sent: list = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: sent.append(a))
    h, oid = _login(client, role="admin")
    try:
        pid = _chat_project(oid)
        r = client.post(f"/api/projects/{pid}/messages",
                        json={"thread": "main", "body": "Consultant here."}, headers=h)
        assert r.status_code == 201, r.text
        assert sent == []  # the human joined - the agent stays out of the way
        r = client.post(f"/api/projects/{pid}/messages",
                        json={"thread": "main",
                              "body": "@agent what incidents do we know of?"}, headers=h)
        assert r.status_code == 201, r.text
        assert [a[0] for a in sent] == ["app.workers.tasks.answer_chat_message"]
    finally:
        _cleanup_org(oid)


# ---------------------------------------------------------------- hub creation

@pytest.fixture
def hub_env():
    with SyncSession() as db:
        admin_org = Organization(name="ChatHub Admin Org")
        db.add(admin_org)
        db.flush()
        admin = User(org_id=admin_org.id, email=f"ch-{uuid.uuid4().hex}@example.com",
                     password_hash="x", role="admin", email_verified=True)
        db.add(admin)
        db.flush()
        plaintext, token_hash = new_api_token()
        db.add(ApiToken(user_id=admin.id, token_hash=token_hash, name="hub", scope="hub"))
        brokered = Organization(name="Hub chat customer", hub_managed=True,
                                credit_balance=50.0,
                                hub_create_key=f"k-{uuid.uuid4().hex}")
        db.add(brokered)
        db.commit()
        env = {"token": plaintext, "admin_org": admin_org.id, "admin": admin.id,
               "brokered_org": brokered.id}
    try:
        yield env
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(
                Project.org_id == env["brokered_org"])).scalars().all()
            if pids:
                db.execute(delete(HubProjectEvent).where(HubProjectEvent.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(CreditTransaction).where(
                    CreditTransaction.project_id.in_(pids)))
                db.execute(delete(Project).where(Project.id.in_(pids)))
            db.execute(delete(CreditTransaction).where(
                CreditTransaction.org_id == env["brokered_org"]))
            db.execute(delete(Organization).where(Organization.id == env["brokered_org"]))
            db.execute(delete(ApiToken).where(ApiToken.user_id == env["admin"]))
            db.execute(delete(User).where(User.id == env["admin"]))
            db.execute(delete(Organization).where(Organization.id == env["admin_org"]))
            db.commit()


def test_hub_creates_chat_project(client, hub_env, chat_unpaused, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    sent: list = []
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: sent.append(a))
    r = client.post("/api/hub/projects",
                    headers={"Authorization": f"Bearer {hub_env['token']}"},
                    json={"spoke_org_id": hub_env["brokered_org"], "kind": "chat",
                          "description": "Hub customer wants to chat.",
                          "hub_ref": f"hubc-{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["kind"] == "chat" and p["status"] == "development"
    assert p["ssh_public_key"] is None and p["subdomain"] is None
    with SyncSession() as db:
        row = db.get(Project, p["id"])
        assert row.source == "hub"
        o = db.get(Organization, hub_env["brokered_org"])
        assert o.credit_balance == pytest.approx(40.0)
        seed = db.query(Message).filter_by(project_id=p["id"], thread="main").one()
        assert seed.author == "customer"
    assert "app.workers.tasks.answer_chat_message" in [a[0] for a in sent]


def test_hub_chat_needs_balance(client, hub_env, chat_unpaused, monkeypatch):
    from app.workers.celery_app import celery as celery_app
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    with SyncSession() as db:
        db.get(Organization, hub_env["brokered_org"]).credit_balance = 3.0
        db.commit()
    r = client.post("/api/hub/projects",
                    headers={"Authorization": f"Bearer {hub_env['token']}"},
                    json={"spoke_org_id": hub_env["brokered_org"], "kind": "chat",
                          "description": "broke hub customer"})
    assert r.status_code == 402
