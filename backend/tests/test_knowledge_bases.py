"""Multi-KB admin management (§KB): seed idempotency, admin-gated CRUD, api_key
encryption + masking, the built-in local/context7 edit rules, removability, and
the local-KB disable short-circuit in services/rag (no embed when disabled).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.core.db import SyncSession
from app.core.encryption import decrypt
from app.core.security import hash_password
from app.main import app
from app.models import KnowledgeBase, Organization, User
from app.seed import seed_knowledge_bases


@pytest.fixture(autouse=True)
def _seeded_kbs():
    """Guarantee the built-in local + context7 rows exist and are enabled for
    every test here, and drop any mcp rows a test leaves behind."""
    seed_knowledge_bases()
    yield
    with SyncSession() as db:
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.kind == "mcp"))
        for kb in db.execute(select(KnowledgeBase)).scalars().all():
            kb.enabled = True
        db.commit()


# ------------------------------------------------------------- seed / rag (DB-level)

def test_seed_is_idempotent_single_builtins():
    seed_knowledge_bases()
    seed_knowledge_bases()
    with SyncSession() as db:
        counts = dict(db.execute(select(KnowledgeBase.kind, func.count())
                                 .group_by(KnowledgeBase.kind)).all())
        assert counts.get("local") == 1
        assert counts.get("context7") == 1
        # Web search is not a knowledge base: a keyed SERP API is a capability,
        # not a corpus, and its rows live in §Tools (test_websearch_tools.py).
        assert "websearch" not in counts
        local = db.execute(select(KnowledgeBase).where(
            KnowledgeBase.kind == "local")).scalar_one()
        assert local.is_removable is False and local.enabled is True


def test_disabled_local_kb_skips_retrieval_without_embedding(monkeypatch):
    from app.services import rag

    calls = []
    monkeypatch.setattr(rag, "_embed_raw",
                        lambda texts: calls.append(texts) or ([[0.0]], {}))
    with SyncSession() as db:
        db.execute(select(KnowledgeBase).where(
            KnowledgeBase.kind == "local")).scalar_one().enabled = False
        db.commit()

    with SyncSession() as db:
        assert rag.retrieve(db, "anything") == ([], [])
        assert rag.search(db, "anything") == []
    assert calls == []  # never embedded -> the disabled KB costs nothing


def test_missing_local_row_treated_as_enabled():
    from app.services import rag

    with SyncSession() as db:
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.kind == "local"))
        db.commit()
    with SyncSession() as db:
        assert rag.local_kb_enabled(db) is True
    seed_knowledge_bases()  # restore for later tests


# ------------------------------------------------------------------- HTTP surface

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    # Heal the async pools an earlier HTTP module left bound to its now-closed
    # loop (see test_org_memory) so a login here doesn't hit a foreign loop.
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _user(role: str):
    email = f"kb-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "kb-secret-123"
    with SyncSession() as db:
        org = Organization(name="KB Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role=role, email_verified=True))
        db.commit()
    return email, pwd


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def test_requires_admin(client):
    # The login limiter is a shared 20/15min per test-client IP, so this module
    # deliberately logs in only twice (here + the CRUD flow below).
    client.cookies.clear()
    assert client.get("/api/admin/knowledge-bases").status_code in (401, 403)
    email, pwd = _user("customer")
    h = _auth(client, email, pwd)
    assert client.get("/api/admin/knowledge-bases", headers=h).status_code == 403


def test_admin_crud_flow(client, monkeypatch):
    email, pwd = _user("admin")
    h = _auth(client, email, pwd)

    # -- list: the two built-ins are present and no key (plaintext or cipher) leaks
    rows = client.get("/api/admin/knowledge-bases", headers=h).json()
    assert {"local", "context7"} <= {r["kind"] for r in rows}
    assert all("api_key" not in r and "api_key_enc" not in r for r in rows)
    local = next(r for r in rows if r["kind"] == "local")

    # -- create mcp: api_key is envelope-encrypted at rest, never returned
    kb = client.post("/api/admin/knowledge-bases", headers=h,
                     json={"name": "Notion", "uri": "https://mcp.notion.com/mcp",
                           "api_key": "ntn_secret"})
    assert kb.status_code == 200, kb.text
    kb = kb.json()
    assert kb["kind"] == "mcp" and kb["has_api_key"] is True and "api_key" not in kb
    with SyncSession() as db:
        row = db.get(KnowledgeBase, kb["id"])
        assert row.api_key_enc and row.api_key_enc != "ntn_secret"
        assert decrypt(row.api_key_enc) == "ntn_secret"

    # -- a non-http(s) uri is rejected
    assert client.post("/api/admin/knowledge-bases", headers=h,
                       json={"name": "x", "uri": "notaurl"}).status_code == 400

    # -- built-in rows accept only `enabled`
    assert client.patch(f"/api/admin/knowledge-bases/{local['id']}", headers=h,
                        json={"enabled": False}).json()["enabled"] is False
    assert client.patch(f"/api/admin/knowledge-bases/{local['id']}", headers=h,
                        json={"name": "hacked"}).status_code == 400
    client.patch(f"/api/admin/knowledge-bases/{local['id']}", headers=h,
                 json={"enabled": True})  # restore

    # -- mcp rows accept every field
    r = client.patch(f"/api/admin/knowledge-bases/{kb['id']}", headers=h,
                     json={"name": "B", "uri": "https://b.example/mcp", "api_key": "k1"})
    assert r.status_code == 200
    assert r.json()["name"] == "B" and r.json()["uri"] == "https://b.example/mcp"

    # -- delete: built-in refused, mcp allowed
    assert client.delete(f"/api/admin/knowledge-bases/{local['id']}",
                         headers=h).status_code == 400
    assert client.delete(f"/api/admin/knowledge-bases/{kb['id']}",
                         headers=h).status_code == 200

def test_new_projects_start_with_no_knowledge_bases():
    """§KB opt-in default: a freshly created project selects NO knowledge bases
    ([]), not the legacy null/all-enabled - KBs are opted in per project."""
    from app.core.db import SyncSession
    from app.models import Organization, Project

    with SyncSession() as db:
        org = Organization(name="KB Default Org")
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai")
        db.add(p)
        db.flush()
        try:
            assert p.kb_ids == []
        finally:
            db.rollback()


# ------------------------------------------------------------- §KB tiers admin

def test_tiers_view_and_block_override(client, monkeypatch):
    """Runs AFTER test_admin_crud_flow and reuses its admin session cookie - this
    module logs in a bounded number of times because the login limiter is shared
    per test-client IP."""
    from app.models import KbBlockClass, KbRulesDigest
    from app.services import kb_classify

    h = {"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrf_token"]}

    rule_text = "every commit message must be prefixed by its issue number"
    fact_text = "the company builds command and control software"
    rule_hash = kb_classify.norm_hash(rule_text)
    fact_hash = kb_classify.norm_hash(fact_text)
    with SyncSession() as db:
        db.execute(delete(KbRulesDigest))
        db.execute(delete(KbBlockClass))
        db.add(KbRulesDigest(root_key="local", content=f"### conv.md\n\n{rule_text}",
                             char_count=len(rule_text)))
        db.add(KbBlockClass(content_hash=rule_hash, content_class="rule", origin="llm"))
        db.commit()
        local_id = db.execute(select(KnowledgeBase.id).where(
            KnowledgeBase.kind == "local")).scalar_one()
        # context7 is a callable source, not a retrieval one - tiers must 400.
        ws_id = db.execute(select(KnowledgeBase.id).where(
            KnowledgeBase.kind == "context7")).scalars().first()

    fake_docs = [
        {"content": rule_text, "file": "local/conv.md", "path": "local/conv.md#0",
         "content_class": "rule", "block_hash": rule_hash},
        {"content": fact_text[:20], "file": "local/about.md", "path": "local/about.md#0",
         "content_class": "fact", "block_hash": fact_hash},
        {"content": fact_text[20:], "file": "local/about.md", "path": "local/about.md#1",
         "content_class": "fact", "block_hash": fact_hash},
        {"content": "other root", "file": "otherroot/x.md", "path": "otherroot/x.md#0",
         "content_class": "fact", "block_hash": "zzz"},
    ]
    from app.services import meili
    monkeypatch.setattr(meili, "iter_kb_docs", lambda fields: iter(fake_docs))
    dispatched = []
    from app.workers.celery_app import celery
    monkeypatch.setattr(celery, "send_task",
                        lambda name, args=None, **kw: dispatched.append((name, args)))

    try:
        # tiers exist for retrieval sources only
        r = client.get(f"/api/admin/knowledge-bases/{ws_id}/tiers", headers=h)
        assert r.status_code == 400

        r = client.get(f"/api/admin/knowledge-bases/{local_id}/tiers", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["counts"] == {"fact": 1, "rule": 1, "procedure": 0}
        assert rule_text in body["digest"]["content"]
        by_hash = {b["block_hash"]: b for b in body["blocks"]}
        assert set(by_hash) == {rule_hash, fact_hash}  # other roots excluded
        assert by_hash[rule_hash]["origin"] == "llm"
        assert by_hash[fact_hash]["origin"] == "auto"
        assert by_hash[fact_hash]["chunks"] == 2
        assert by_hash[fact_hash]["excerpt"] == fact_text[:20]  # chunk #0, not #1
        assert (body["total"], body["page"], body["per_page"]) == (2, 1, 50)

        # class filter narrows blocks and total; counts stay global (stable chips)
        r = client.get(f"/api/admin/knowledge-bases/{local_id}/tiers"
                       "?content_class=fact&per_page=1", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["counts"] == {"fact": 1, "rule": 1, "procedure": 0}
        assert body["total"] == 1
        assert [b["block_hash"] for b in body["blocks"]] == [fact_hash]
        # a page past the filtered window is empty but keeps the same total
        r = client.get(f"/api/admin/knowledge-bases/{local_id}/tiers"
                       "?content_class=fact&per_page=1&page=2", headers=h)
        assert r.json()["blocks"] == [] and r.json()["total"] == 1
        # an unknown class is rejected
        assert client.get(f"/api/admin/knowledge-bases/{local_id}/tiers"
                          "?content_class=nope", headers=h).status_code == 422

        # pin a class -> override row + forced reindex dispatched
        r = client.put(f"/api/admin/knowledge-bases/blocks/{fact_hash}",
                       json={"content_class": "rule"}, headers=h)
        assert r.status_code == 200, r.text
        with SyncSession() as db:
            row = db.get(KbBlockClass, fact_hash)
            assert row.content_class == "rule" and row.origin == "override"
        assert dispatched and dispatched[-1] == ("app.workers.tasks.ingest_knowledge", [True])

        # pinning an already-cached block flips it to override
        r = client.put(f"/api/admin/knowledge-bases/blocks/{rule_hash}",
                       json={"content_class": "fact"}, headers=h)
        assert r.status_code == 200
        with SyncSession() as db:
            assert db.get(KbBlockClass, rule_hash).origin == "override"

        # revert clears ONLY override rows
        r = client.delete(f"/api/admin/knowledge-bases/blocks/{fact_hash}", headers=h)
        assert r.status_code == 200
        with SyncSession() as db:
            assert db.get(KbBlockClass, fact_hash) is None
        assert client.delete(f"/api/admin/knowledge-bases/blocks/{fact_hash}",
                             headers=h).status_code == 404
    finally:
        with SyncSession() as db:
            db.execute(delete(KbRulesDigest))
            db.execute(delete(KbBlockClass))
            db.commit()
