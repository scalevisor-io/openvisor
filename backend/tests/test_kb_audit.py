"""KB-leak self-audit tests: report shape, the load-bearing "no raw text ever
leaves the audit" invariant, clean-KB behaviour, scoring/threshold math, the
unavailable-stack soft failure, and hub-token scope enforcement on the endpoint.
The LLM/RAG stack isn't reachable in tests, so the real retrieval+synthesis is
stubbed (mirroring test_knowledge.py); HTTP paths reuse test_hub.py's throwaway
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

# A verbatim run at/over the guard threshold, and a paraphrase that shares no long
# run with it. Reused across the leak / no-leak cases.
SECRET_SPAN = ("alpha bravo charlie delta echo foxtrot golf hotel india juliett "
               "kilo lima mike november oscar papa quebec")
KB_BODY = f"Confidential reference material. The ordered tokens are {SECRET_SPAN}. End."


# ---- pure: max verbatim overlap ----

def test_max_overlap_counts_longest_run():
    # The whole 17-word span is present verbatim in the chunk.
    n = kb_audit._max_verbatim_overlap(f"Note: {SECRET_SPAN} and more.", [KB_BODY])
    assert n >= kb_audit.LEAK_THRESHOLD
    # A paraphrase shares no long verbatim run.
    para = "The passage lists a sequence of NATO phonetic code words in order."
    assert kb_audit._max_verbatim_overlap(para, [KB_BODY]) < kb_audit.LEAK_THRESHOLD
    # No chunks / empty answer -> zero, never a crash.
    assert kb_audit._max_verbatim_overlap("anything at all", []) == 0
    assert kb_audit._max_verbatim_overlap("", [KB_BODY]) == 0


def test_leak_threshold_tracks_the_live_guard():
    # The audit must flag at exactly the bar the live search_knowledge guard uses.
    assert kb_audit.LEAK_THRESHOLD == knowledge.MIN_VERBATIM_RUN


# ---- pure: content-recall semantic signal (IKEA/Pirate blind spot) ----

def test_content_recall_catches_paraphrase_without_verbatim_run():
    chunk = ("Sovereign hosting mandates data residency inside approved jurisdictions, "
             "isolated tenancy, encrypted storage, and audited access controls.")
    # Reproduces the chunk's distinctive vocabulary REWORDED, no long verbatim run.
    answer = ("Approved jurisdictions require residency; keep tenancy isolated, storage "
              "encrypted, access audited, under sovereign hosting controls.")
    # The verbatim metric is blind here...
    assert kb_audit._max_verbatim_overlap(answer, [chunk]) < kb_audit.LEAK_THRESHOLD
    # ...but content recall sees the semantic reconstruction.
    assert kb_audit._max_content_recall(answer, [chunk]) >= kb_audit.CONTENT_RECALL_THRESHOLD
    # empty inputs never crash / never flag
    assert kb_audit._max_content_recall("", [chunk]) == 0.0
    assert kb_audit._max_content_recall(answer, []) == 0.0
    # a genuinely unrelated answer stays well under the floor
    assert kb_audit._max_content_recall(
        "It depends on your setup; plan carefully and review the basics.", [chunk]) < 0.5


# ---- pure: scoring + levels ----

def test_risk_score_weights_and_levels():
    exfil = {"probe": "p", "category": "direct-exfil", "leaked": True, "max_overlap_words": 20}
    sweep = {"probe": "s", "category": "topic-sweep", "leaked": False, "max_overlap_words": 3}
    # 1.0 leaked over (1.0 + 0.5) total = 0.6667 -> high.
    score = kb_audit._risk_score([exfil, sweep])
    assert score == pytest.approx(1.0 / 1.5, abs=1e-4)
    assert kb_audit._level(score) == "high"
    # Nothing leaked -> 0.0 -> low.
    clean = [{**exfil, "leaked": False}, sweep]
    assert kb_audit._risk_score(clean) == 0.0
    assert kb_audit._level(0.0) == "low"
    # Only a low-weight sweep leaks: 0.5 / (1.0 + 0.5) = 0.3333 -> medium.
    sweep_leaked = {**sweep, "leaked": True}
    mid = kb_audit._risk_score([{**exfil, "leaked": False}, sweep_leaked])
    assert mid == pytest.approx(0.5 / 1.5, abs=1e-4)
    assert kb_audit._level(mid) == "medium"


def test_level_boundaries():
    assert kb_audit._level(0.0) == "low"
    assert kb_audit._level(kb_audit.LEVEL_LOW_MAX - 1e-6) == "low"
    assert kb_audit._level(kb_audit.LEVEL_LOW_MAX) == "medium"
    assert kb_audit._level(kb_audit.LEVEL_MEDIUM_MAX - 1e-6) == "medium"
    assert kb_audit._level(kb_audit.LEVEL_MEDIUM_MAX) == "high"


# ---- run_audit: report shape + no-raw-text invariant ----

def _stub_probes(monkeypatch, answer_by_category):
    """Stub the retrieval+synthesis path so run_audit uses controlled answers
    without touching the real LLM/embedding stack. `answer_by_category` maps a
    probe category to the (answer, chunk_texts) it should produce."""
    monkeypatch.setattr(kb_audit, "_topic_seeds", lambda db, n=3: ["topicword"])

    cat_by_query = {}

    real_build = kb_audit._build_probes

    def build(db):
        probes = real_build(db)
        for p in probes:
            cat_by_query[p["query"]] = p["category"]
        return probes

    monkeypatch.setattr(kb_audit, "_build_probes", build)

    def run_probe(db, query, k):
        # adaptive hops build follow-up queries at run time (not registered by
        # build()); treat any such unregistered query as the adaptive-sweep category.
        return answer_by_category[cat_by_query.get(query, "adaptive-sweep")]

    monkeypatch.setattr(kb_audit, "_run_probe", run_probe)


def test_run_audit_shape_and_never_leaks_raw_text(monkeypatch):
    import json

    # Every probe "leaks" the secret span verbatim - the worst case. The report
    # must still carry only flags/counts, never the answer or chunk text.
    leak = (f"Here it is: {SECRET_SPAN}.", [KB_BODY])
    _stub_probes(monkeypatch, {c: leak for c in
                               ("direct-exfil", "prompt-injection", "context-dump",
                                "topic-sweep", "benign-extraction", "adaptive-sweep")})

    with SyncSession() as db:
        report = kb_audit.run_audit(db)

    # Exactly the contracted keys on the success path.
    assert set(report) == {"risk_score", "level", "checked_at", "probe_count",
                           "leaked_count", "findings"}
    assert report["level"] == "high"
    assert report["probe_count"] == len(report["findings"]) > 0
    assert report["leaked_count"] == report["probe_count"]
    for f in report["findings"]:
        assert set(f) == {"probe", "category", "leaked", "max_overlap_words", "max_recall_pct"}
        assert isinstance(f["leaked"], bool) and isinstance(f["max_overlap_words"], int)
        assert isinstance(f["max_recall_pct"], float)

    # Load-bearing: no KB text (nor any answer text) survives into the report.
    blob = json.dumps(report)
    assert SECRET_SPAN not in blob
    assert "foxtrot golf hotel" not in blob
    assert "Confidential reference material" not in blob


def test_run_audit_clean_when_nothing_leaks(monkeypatch):
    para = ("The material is summarized in the auditor's own words with a citation [1].", [KB_BODY])
    _stub_probes(monkeypatch, {c: para for c in
                               ("direct-exfil", "prompt-injection", "context-dump",
                                "topic-sweep", "benign-extraction", "adaptive-sweep")})
    with SyncSession() as db:
        report = kb_audit.run_audit(db)
    assert report["level"] == "low"
    assert report["leaked_count"] == 0
    assert report["risk_score"] == 0.0


def test_run_audit_flags_semantic_leak_on_benign_and_adaptive(monkeypatch):
    # Benign/adaptive probes get a paraphrased answer that reproduces the chunk's
    # vocabulary with NO long verbatim run - a verbatim-only audit would call this
    # clean; the content-recall signal must flag it.
    chunk = ("Sovereign hosting mandates data residency inside approved jurisdictions, "
             "isolated tenancy, encrypted storage, and audited access controls.")
    semantic_leak = ("Approved jurisdictions require residency; keep tenancy isolated, "
                     "storage encrypted, access audited, under sovereign hosting controls.",
                     [chunk])
    clean = ("It depends on your setup; plan carefully and review the basics.", [chunk])
    _stub_probes(monkeypatch, {
        "direct-exfil": clean, "prompt-injection": clean, "context-dump": clean,
        "topic-sweep": clean, "benign-extraction": semantic_leak,
        "adaptive-sweep": semantic_leak})

    with SyncSession() as db:
        report = kb_audit.run_audit(db)

    leaked = [f for f in report["findings"] if f["leaked"]]
    assert {f["category"] for f in leaked} == {"benign-extraction", "adaptive-sweep"}
    # flagged with no long verbatim run -> the new semantic signal did it
    for f in leaked:
        assert f["max_overlap_words"] < kb_audit.LEAK_THRESHOLD
        assert f["max_recall_pct"] >= kb_audit.CONTENT_RECALL_THRESHOLD


def test_run_audit_empty_kb_is_low_no_llm(monkeypatch):
    # No KB seeds -> no topic-sweep probes; empty retrieval short-circuits with no
    # synthesis call at all. A clean/empty KB must report low / zero leaks.
    monkeypatch.setattr(kb_audit, "_topic_seeds", lambda db, n=3: [])
    monkeypatch.setattr("app.services.rag.retrieve",
                        lambda db, q, k: ([], []))

    def _no_synth(*a, **k):
        pytest.fail("empty retrieval must not call synthesis")

    monkeypatch.setattr(knowledge, "_synthesize", _no_synth)
    with SyncSession() as db:
        report = kb_audit.run_audit(db)
    assert report["level"] == "low"
    assert report["leaked_count"] == 0
    assert report["probe_count"] == len(kb_audit.STATIC_PROBES)


def test_run_audit_unknown_when_stack_unavailable(monkeypatch):
    monkeypatch.setattr(kb_audit, "_topic_seeds", lambda db, n=3: [])

    def _down(db, q, k):
        raise knowledge.llm.LLMUnavailable("provider down")

    monkeypatch.setattr(kb_audit, "_run_probe", _down)
    with SyncSession() as db:
        report = kb_audit.run_audit(db)
    assert report["level"] == "unknown"
    assert report["probe_count"] == 0 and report["leaked_count"] == 0
    assert report["findings"] == []
    assert report["error"]  # a note is attached


# ---- HTTP endpoint: hub-token scope enforcement ----

@pytest.fixture(scope="module")
def client():
    # The module-level async engine may still hold asyncpg connections bound to an
    # earlier TestClient module's event loop (test_hub); abandon them so this
    # module's loop makes fresh ones instead of hitting "Event loop is closed".
    import asyncio

    from app.core.db import engine as _async_engine
    asyncio.run(_async_engine.dispose(close=False))
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
    with SyncSession() as db:
        org = Organization(name="KB Audit Test Org", credit_balance=50.0)
        db.add(org)
        db.flush()
        user = User(org_id=org.id, email=f"kbaudit-{uuid.uuid4().hex}@example.com",
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


def test_endpoint_requires_hub_token(client, hub_env):
    # A user-scoped token must be rejected before any audit runs.
    r = client.post("/api/hub/kb-audit", headers=_h(hub_env["user_token"]))
    assert r.status_code == 403


def test_endpoint_returns_report(client, hub_env, monkeypatch):
    from app.api import hub as hub_api

    monkeypatch.setattr(hub_api.kb_audit, "run_audit",
                        lambda db: {"risk_score": 0.0, "level": "low",
                                    "checked_at": "2026-01-01T00:00:00+00:00",
                                    "probe_count": 8, "leaked_count": 0, "findings": []})
    r = client.post("/api/hub/kb-audit", headers=_h(hub_env["hub_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["level"] == "low"
