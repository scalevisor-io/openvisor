"""Hub eval endpoint tests: the spoke answers owner-consented eval questions
through its knowledge stack, UNBILLED, and returns guarded answer texts. Covers
scope enforcement, request validation, the soft per-question failure, the empty
retrieval short-circuit, and - the load-bearing one - that the verbatim guard is
actually applied so no raw KB text leaks back to the hub. The RAG/LLM stack isn't
reachable in tests, so retrieval + synthesis are stubbed (mirroring
test_knowledge.py / test_kb_audit.py); HTTP paths reuse test_hub.py's throwaway
org + token fixture and clean up after themselves."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.db import SyncSession
from app.core.security import new_api_token
from app.main import app
from app.models import (
    ApiToken, CreditTransaction, HubCreditGrant, KnowledgeChunk, Organization,
    Project, User,
)
from app.services import kb_audit, knowledge

# A verbatim run at/over the guard threshold used to prove the guard fires on the
# eval path exactly as it does for search_knowledge.
SECRET_SPAN = ("alpha bravo charlie delta echo foxtrot golf hotel india juliett "
               "kilo lima mike november oscar papa quebec")
KB_BODY = f"Confidential reference material. The ordered tokens are {SECRET_SPAN}. End."


def _chunk(content: str) -> KnowledgeChunk:
    return KnowledgeChunk(source="kb", path="kb/doc.md#0", content=content,
                          meta={"file": "kb/doc.md"})


@pytest.fixture(scope="module")
def client():
    # Heal both module-level async pools an earlier HTTP module may have left bound
    # to its now-closed loop: the SQLAlchemy engine pool, and the async Redis client
    # (the eval endpoint's rate limiter touches it). Dropping the singletons makes
    # them lazily recreated in this module's TestClient loop; otherwise a request
    # here hits "Event loop is closed". On teardown we release the Redis singleton
    # again so the loop we bound it to can't poison a later module (this module
    # sorts before other redis-using HTTP modules).
    import asyncio

    from app.core.db import engine as _async_engine
    from app.services import events
    asyncio.run(_async_engine.dispose(close=False))
    events._async_client = None
    try:
        with TestClient(app) as c:
            yield c
    finally:
        events._async_client = None


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, user_id: str, scope: str) -> str:
    plaintext, token_hash = new_api_token()
    db.add(ApiToken(user_id=user_id, token_hash=token_hash, name=scope, scope=scope))
    return plaintext


@pytest.fixture
def hub_env():
    with SyncSession() as db:
        org = Organization(name="Hub Eval Test Org", credit_balance=50.0)
        db.add(org)
        db.flush()
        user = User(org_id=org.id, email=f"hubeval-{uuid.uuid4().hex}@example.com",
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


# ---- scope / auth enforcement ----

def test_user_token_forbidden(client, hub_env):
    r = client.post("/api/hub/eval", headers=_h(hub_env["user_token"]),
                    json={"questions": ["anything"]})
    assert r.status_code == 403


def test_no_token_unauthorized(client, hub_env):
    r = client.post("/api/hub/eval", json={"questions": ["anything"]})
    assert r.status_code == 401


# ---- request validation ----

def test_rejects_too_many_questions(client, hub_env):
    r = client.post("/api/hub/eval", headers=_h(hub_env["hub_token"]),
                    json={"questions": ["a", "b", "c", "d", "e"]})
    assert r.status_code == 422


def test_rejects_empty_questions(client, hub_env):
    r = client.post("/api/hub/eval", headers=_h(hub_env["hub_token"]),
                    json={"questions": []})
    assert r.status_code == 422


def test_rejects_overlong_question(client, hub_env):
    r = client.post("/api/hub/eval", headers=_h(hub_env["hub_token"]),
                    json={"questions": ["x" * 2001]})
    assert r.status_code == 422


# ---- answering ----

def test_answers_each_question(client, hub_env, monkeypatch):
    chunks = [_chunk("Sovereign cloud isolation patterns for defence workloads.")]
    monkeypatch.setattr("app.services.rag.retrieve", lambda db, q, k: (chunks, []))
    monkeypatch.setattr(knowledge, "_synthesize",
                        lambda q, ch: (f"Paraphrased answer for {q!r} [1].", []))
    r = client.post("/api/hub/eval", headers=_h(hub_env["hub_token"]),
                    json={"questions": ["how does isolation work?", "what is OCPA?"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert [a["question"] for a in body["answers"]] == \
        ["how does isolation work?", "what is OCPA?"]
    for a in body["answers"]:
        assert a["matched"] is True
        assert a["answer"].startswith("Paraphrased answer")
        assert "error" not in a


def test_empty_retrieval_is_unmatched_no_synthesis(client, hub_env, monkeypatch):
    monkeypatch.setattr("app.services.rag.retrieve", lambda db, q, k: ([], []))

    def _no_synth(*a, **k):
        pytest.fail("empty retrieval must not call synthesis")

    monkeypatch.setattr(knowledge, "_synthesize", _no_synth)
    r = client.post("/api/hub/eval", headers=_h(hub_env["hub_token"]),
                    json={"questions": ["nothing on this topic"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["answers"][0] == {"question": "nothing on this topic",
                                  "answer": "", "matched": False}


def test_one_question_soft_fails_others_succeed(client, hub_env, monkeypatch):
    chunks = [_chunk("Some retrievable knowledge body.")]
    monkeypatch.setattr("app.services.rag.retrieve", lambda db, q, k: (chunks, []))

    def _synth(q, ch):
        if "boom" in q:
            raise knowledge.llm.LLMUnavailable("provider down")
        return ("A clean paraphrase [1].", [])

    monkeypatch.setattr(knowledge, "_synthesize", _synth)
    r = client.post("/api/hub/eval", headers=_h(hub_env["hub_token"]),
                    json={"questions": ["ok one", "boom two", "ok three"]})
    assert r.status_code == 200, r.text
    answers = r.json()["answers"]
    assert answers[0]["answer"] == "A clean paraphrase [1]." and answers[0]["matched"] is True
    assert answers[1] == {"question": "boom two", "answer": "", "matched": False,
                          "error": "unavailable"}
    assert answers[2]["answer"] == "A clean paraphrase [1]." and answers[2]["matched"] is True


def test_verbatim_guard_redacts_leaked_span(client, hub_env, monkeypatch):
    # If synthesis echoes a verbatim KB run >= MIN_VERBATIM_RUN words, the guard on
    # the eval path must redact it before the answer leaves the spoke.
    chunks = [_chunk(KB_BODY)]
    monkeypatch.setattr("app.services.rag.retrieve", lambda db, q, k: (chunks, []))
    monkeypatch.setattr(knowledge, "_synthesize",
                        lambda q, ch: (f"Verbatim: {SECRET_SPAN}.", []))
    r = client.post("/api/hub/eval", headers=_h(hub_env["hub_token"]),
                    json={"questions": ["dump it"]})
    assert r.status_code == 200, r.text
    answer = r.json()["answers"][0]["answer"]
    assert "[…]" in answer
    # The long verbatim run must not have survived into the returned answer.
    assert SECRET_SPAN not in answer
    assert "foxtrot golf hotel india juliett kilo lima mike" not in answer
