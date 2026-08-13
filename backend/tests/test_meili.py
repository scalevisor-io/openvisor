"""Meilisearch KB retrieval tests.

Two layers:
  - unit tests driving the real services/meili.py request-building code against an
    in-memory httpx.MockTransport that emulates Meilisearch (index create, settings,
    delete-all + add, hybrid search, document pagination), plus the rag.py wiring
    that routes the KB path to Meili while keeping the query embedding billable;
  - one integration test that indexes a tiny fixture into a LIVE Meilisearch
    (MEILI_URL) with user-provided vectors and asserts hybrid search ranks the
    expected doc first. It skips when no Meilisearch is reachable.
"""
import json

import httpx
import pytest

from app.services import meili, rag


# ---- in-memory fake Meilisearch (exercises the real client code) ----

def _fake_meili():
    """Return (state, handler): a stateful httpx.MockTransport handler emulating the
    subset of the Meilisearch API services/meili.py uses, INDEX-NAME-AWARE so it
    exercises the atomic reindex (build a temp `kb_<ts>` index, then `/swap-indexes`
    into `kb`, then drop the temp). `state['idx']` maps index name -> {doc_id: doc};
    `kb` is only ever read by the app, and the swap keeps it complete."""
    state = {"idx": {}, "experimental": {}}

    def _name(path: str) -> str:
        # /indexes/<name>[/sub] -> <name>
        return path.split("/indexes/", 1)[1].split("/", 1)[0]

    def handler(request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if path == "/experimental-features":
            return httpx.Response(200, json=state["experimental"])
        if path.startswith("/tasks/"):
            return httpx.Response(200, json={"status": "succeeded"})
        if path == "/indexes" and method == "POST":
            state["idx"].setdefault(json.loads(request.content)["uid"], {})
            return httpx.Response(202, json={"taskUid": 1})
        if path == "/swap-indexes" and method == "POST":
            a, b = json.loads(request.content)[0]["indexes"]
            state["idx"][a], state["idx"][b] = (
                state["idx"].get(b, {}), state["idx"].get(a, {}))
            return httpx.Response(202, json={"taskUid": 5})
        if path.startswith("/indexes/"):
            name = _name(path)
            if path == f"/indexes/{name}" and method == "GET":
                return (httpx.Response(200, json={"uid": name}) if name in state["idx"]
                        else httpx.Response(404, json={"code": "index_not_found"}))
            if path == f"/indexes/{name}" and method == "DELETE":
                state["idx"].pop(name, None)
                return httpx.Response(202, json={"taskUid": 6})
            if path == f"/indexes/{name}/settings" and method == "PATCH":
                return httpx.Response(202, json={"taskUid": 2})
            if path == f"/indexes/{name}/stats" and method == "GET":
                if name not in state["idx"]:
                    return httpx.Response(404, json={"code": "index_not_found"})
                return httpx.Response(200, json={"numberOfDocuments": len(state["idx"][name])})
            if path == f"/indexes/{name}/documents" and method == "DELETE":
                state["idx"].get(name, {}).clear()
                return httpx.Response(202, json={"taskUid": 3})
            if path == f"/indexes/{name}/documents" and method == "POST":
                bucket = state["idx"].setdefault(name, {})
                for d in json.loads(request.content):
                    bucket[d["id"]] = d
                return httpx.Response(202, json={"taskUid": 4})
            if path == f"/indexes/{name}/documents" and method == "GET":
                if name not in state["idx"]:
                    return httpx.Response(404, json={"code": "index_not_found"})
                params = request.url.params
                limit, offset = int(params.get("limit", 1000)), int(params.get("offset", 0))
                docs = list(state["idx"][name].values())[offset:offset + limit]
                return httpx.Response(200, json={
                    "results": [{"content": d["content"]} for d in docs],
                    "limit": limit, "offset": offset, "total": len(state["idx"][name])})
            if path == f"/indexes/{name}/search" and method == "POST":
                if name not in state["idx"]:
                    return httpx.Response(404, json={"code": "index_not_found"})
                body = json.loads(request.content)
                q = (body.get("q") or "").lower()
                docs = state["idx"][name].values()
                matched = [d for d in docs if q and q in d["content"].lower()]
                hits = (matched or list(docs))[: body.get("limit", 20)]
                return httpx.Response(200, json={"hits": [
                    {"content": d["content"], "source": d["source"],
                     "path": d["path"], "file": d["file"]} for d in hits]})
        return httpx.Response(404, json={"code": "index_not_found", "message": "not found"})

    return state, handler


@pytest.fixture
def fake_meili(monkeypatch):
    state, handler = _fake_meili()

    def _client():
        return httpx.Client(base_url="http://meili.test",
                            transport=httpx.MockTransport(handler),
                            headers={"Authorization": "Bearer test"})

    monkeypatch.setattr(meili, "_client", _client)
    return state


def _doc(id_, content, path):
    return {"id": id_, "content": content, "source": "kb", "path": path,
            "file": path.split("#")[0], "_vectors": {meili.EMBEDDER: [0.0] * meili.EMBED_DIM}}


def test_reindex_replaces_then_search_and_all_docs(fake_meili):
    meili.reindex_kb([
        _doc("a", "sovereign EU hosting patterns", "kb/hosting.md#0"),
        _doc("b", "discovery phase structure guidance", "kb/discovery.md#0"),
    ])
    assert len(fake_meili["idx"]["kb"]) == 2

    hits = meili.search_hybrid([0.0] * meili.EMBED_DIM, "sovereign", k=5)
    assert hits and hits[0]["content"] == "sovereign EU hosting patterns"
    assert hits[0]["source"] == "kb" and hits[0]["file"] == "kb/hosting.md"

    # all_kb_docs yields every stored content; a full reindex drops the old set.
    assert set(meili.all_kb_docs()) == {"sovereign EU hosting patterns",
                                        "discovery phase structure guidance"}
    meili.reindex_kb([_doc("c", "only this now", "kb/new.md#0")])
    assert list(meili.all_kb_docs()) == ["only this now"]


def test_reindex_is_atomic_swap_and_cleans_temp(fake_meili):
    """The reindex builds a temp index and atomically swaps it into `kb`, so `kb` is
    never empty/partial, and the swapped-out temp index is dropped afterwards - only
    `kb` remains. This is the confidentiality/RAG/billing fix (no empty-index window)."""
    meili.reindex_kb([_doc("a", "first corpus", "kb/a.md#0")])
    assert set(fake_meili["idx"]) == {"kb"}  # temp cleaned up, only kb left
    assert meili.kb_doc_count() == 1

    meili.reindex_kb([_doc("b", "second corpus", "kb/b.md#0"),
                      _doc("c", "more", "kb/c.md#0")])
    # kb holds exactly the new set; no leftover temp indexes accumulate.
    assert set(fake_meili["idx"]) == {"kb"}
    assert meili.kb_doc_count() == 2
    assert set(meili.all_kb_docs()) == {"second corpus", "more"}


def test_search_missing_index_is_empty(monkeypatch):
    def _client():
        return httpx.Client(base_url="http://meili.test", headers={},
                            transport=httpx.MockTransport(
                                lambda r: httpx.Response(404, json={"code": "index_not_found"})))
    monkeypatch.setattr(meili, "_client", _client)
    assert meili.search_hybrid([0.0] * meili.EMBED_DIM, "anything", k=5) == []
    assert list(meili.all_kb_docs()) == []


def test_configure_index_skips_create_when_present(monkeypatch):
    # Index creation is async (a duplicate POST yields a task that fails), so
    # _configure_index must NOT re-create when GET reports the index already exists.
    calls = {"post_indexes": 0}

    def handler(request):
        if request.url.path == f"/indexes/{meili.INDEX}" and request.method == "GET":
            return httpx.Response(200, json={"uid": meili.INDEX})  # already exists
        if request.url.path == "/indexes" and request.method == "POST":
            calls["post_indexes"] += 1
            return httpx.Response(202, json={"taskUid": 1})
        if request.url.path.startswith("/tasks/"):
            return httpx.Response(200, json={"status": "succeeded"})
        return httpx.Response(202, json={"taskUid": 9})  # settings PATCH
    with httpx.Client(base_url="http://meili.test",
                      transport=httpx.MockTransport(handler)) as c:
        meili._configure_index(c, meili.INDEX)  # must not raise
    assert calls["post_indexes"] == 0  # never re-created


# ---- rag.py wiring: KB path -> Meili, query embedding still billable ----

def test_retrieve_routes_kb_to_meili_and_keeps_usage(monkeypatch):
    emb_usage = {"model": "mistral-embed", "input_tokens": 7, "output_tokens": 0}
    monkeypatch.setattr(rag, "local_kb_enabled", lambda db: True)
    monkeypatch.setattr(rag, "_embed_raw", lambda texts: ([[0.1] * rag.EMBED_DIM], emb_usage))
    monkeypatch.setattr(meili, "search_hybrid", lambda vec, q, k, tags=None: [
        {"content": "chunk body", "source": "kb", "path": "kb/x.md#0", "file": "kb/x.md"}])

    hits, usages = rag.retrieve(object(), "how to structure a discovery phase", k=6)
    assert usages == [emb_usage]  # the billable query-embed usage survives
    assert len(hits) == 1
    h = hits[0]
    # the hit exposes exactly the attributes every caller reads
    assert (h.content, h.source, h.path, h.file) == ("chunk body", "kb", "kb/x.md#0", "kb/x.md")
    assert h.meta == {"file": "kb/x.md", "block_hash": None}


def test_search_default_source_uses_meili(monkeypatch):
    monkeypatch.setattr(rag, "local_kb_enabled", lambda db: True)
    monkeypatch.setattr(rag, "_embed_raw", lambda texts: ([[0.2] * rag.EMBED_DIM], {}))
    called = {}

    def _search(vec, q, k, tags=None):
        called["q"] = q
        return [{"content": "c", "source": "kb", "path": "p", "file": "p"}]
    monkeypatch.setattr(meili, "search_hybrid", _search)

    hits = rag.search(object(), "sovereign hosting", k=6)
    assert called["q"] == "sovereign hosting"
    assert hits[0].content == "c"


# ---- anti-extraction retrieval floor (settings.kb_retrieval_min_score) ----

def test_search_hybrid_requests_and_returns_ranking_score(monkeypatch):
    """search_hybrid asks Meili for showRankingScore and surfaces _rankingScore as
    `score` so the rag floor can act on it."""
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"hits": [
            {"content": "c", "source": "kb", "path": "kb/a.md#0",
             "file": "kb/a.md", "_rankingScore": 0.83}]})

    monkeypatch.setattr(meili, "_client", lambda: httpx.Client(
        base_url="http://meili.test", transport=httpx.MockTransport(handler)))

    hits = meili.search_hybrid([0.0] * meili.EMBED_DIM, "q", k=5)
    assert captured["body"]["showRankingScore"] is True
    assert hits[0]["score"] == 0.83


def test_above_threshold_drops_low_scores_keeps_unscored(monkeypatch):
    hits = [rag.KBHit(content="hi", score=0.9),
            rag.KBHit(content="lo", score=0.2),
            rag.KBHit(content="unscored", score=None)]

    monkeypatch.setattr(rag.settings, "kb_retrieval_min_score", 0.5, raising=False)
    kept = rag._above_threshold(hits)
    # low-relevance chunk dropped; high-score and unscored (fail-open) kept
    assert [h.content for h in kept] == ["hi", "unscored"]

    monkeypatch.setattr(rag.settings, "kb_retrieval_min_score", 0.0, raising=False)
    assert len(rag._above_threshold(hits)) == 3  # floor 0 disables it


def test_retrieve_applies_retrieval_floor(monkeypatch):
    """An off-topic corpus-sweep query still gets top-k hits from Meili, but the floor
    strips the low-score ones before they reach the caller (the extraction defence)."""
    monkeypatch.setattr(rag, "local_kb_enabled", lambda db: True)
    monkeypatch.setattr(rag, "_embed_raw", lambda texts: ([[0.1] * rag.EMBED_DIM], {}))
    monkeypatch.setattr(rag.settings, "kb_retrieval_min_score", 0.5, raising=False)
    monkeypatch.setattr(meili, "search_hybrid", lambda vec, q, k, tags=None: [
        {"content": "relevant", "source": "kb", "path": "kb/a.md#0",
         "file": "kb/a.md", "score": 0.9},
        {"content": "off-topic sweep", "source": "kb", "path": "kb/b.md#0",
         "file": "kb/b.md", "score": 0.1}])

    hits, _usages = rag.retrieve(object(), "legit question", k=6)
    assert [h.content for h in hits] == ["relevant"]


def test_ingest_builds_vector_docs_and_reindexes(monkeypatch, tmp_path):
    from app.services import kb_classify

    (tmp_path / "guide.md").write_text("Sovereign hosting for the EU. " * 5)
    monkeypatch.setattr(rag, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(rag, "embed", lambda texts: [[0.5] * rag.EMBED_DIM for _ in texts])
    monkeypatch.setattr(kb_classify, "_llm_classify", lambda blocks: None)  # -> fact
    captured = {}

    def _reindex(docs):
        captured["docs"] = docs
        return len(docs)
    monkeypatch.setattr(meili, "reindex_kb", _reindex)

    n = rag.ingest_knowledge_repo()
    assert n == 1
    doc = captured["docs"][0]
    # The single local root is namespaced under the "local" root key.
    assert doc["source"] == "kb" and doc["file"] == "local/guide.md"
    assert doc["path"] == "local/guide.md#0"
    assert doc["_vectors"][meili.EMBEDDER] == [0.5] * rag.EMBED_DIM
    assert isinstance(doc["id"], str) and doc["id"]  # opaque hashed id (Meili-safe)


def test_ingest_knowledge_selfheals_empty_index(monkeypatch):
    """The beat skips re-embed on an unchanged /knowledge fingerprint - BUT only when
    the live index still holds the corpus. An unchanged fingerprint with an empty
    index (Meili volume reset / PVC recreate) must self-heal by reindexing, else the
    KB is silently empty forever."""
    from app.services import app_settings, meili as meilimod, rag as ragmod
    from app.workers import tasks

    from app.services import kb_git
    monkeypatch.setattr(kb_git, "prepare_roots", lambda db: ([], 0))
    monkeypatch.setattr(ragmod, "kb_tree_fingerprint", lambda roots=None: "fp-x")
    monkeypatch.setattr(app_settings, "get_setting_sync", lambda db, key, default=None: "fp-x")
    monkeypatch.setattr(app_settings, "set_setting_sync", lambda db, key, val: None)
    reindexed = {"n": 0}
    monkeypatch.setattr(ragmod, "ingest_knowledge_repo",
                        lambda roots=None, had_source_errors=False:
                        (reindexed.__setitem__("n", reindexed["n"] + 1) or 7))

    # unchanged fingerprint + healthy (non-empty) index -> skip, no reindex.
    monkeypatch.setattr(meilimod, "kb_doc_count", lambda: 5)
    assert tasks.ingest_knowledge() == -1
    assert reindexed["n"] == 0

    # unchanged fingerprint but EMPTY index -> self-heal reindex.
    monkeypatch.setattr(meilimod, "kb_doc_count", lambda: 0)
    assert tasks.ingest_knowledge() == 7
    assert reindexed["n"] == 1


# ---- integration: a live Meilisearch ranks the expected doc first ----

def _meili_reachable() -> bool:
    try:
        with meili._client() as c:
            return c.get("/health").status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _meili_reachable(), reason="no live Meilisearch at MEILI_URL")
def test_live_hybrid_search_ranks_expected_doc():
    # User-provided vectors (no embedding API needed): doc A is both the keyword AND
    # the vector match for the query, so hybrid must rank it first.
    lo = [0.1] * meili.EMBED_DIM
    hi = [0.9] * meili.EMBED_DIM
    docs = [
        {"id": "itest-a", "content": "sovereign EU hosting and data residency",
         "source": "kb", "path": "it/a.md#0", "file": "it/a.md",
         "_vectors": {meili.EMBEDDER: lo}},
        {"id": "itest-b", "content": "unrelated marketing copy about widgets",
         "source": "kb", "path": "it/b.md#0", "file": "it/b.md",
         "_vectors": {meili.EMBEDDER: hi}},
    ]
    try:
        meili.reindex_kb(docs)
        hits = meili.search_hybrid(lo, "sovereign hosting", k=5)
        assert hits, "hybrid search returned no hits"
        assert hits[0]["path"] == "it/a.md#0"
        contents = set(meili.all_kb_docs())
        assert "sovereign EU hosting and data residency" in contents
    finally:
        # leave the index clean so a subsequent real ingest starts fresh
        with meili._client() as c:
            c.delete(f"/indexes/{meili.INDEX}/documents")


@pytest.mark.skipif(not _meili_reachable(), reason="no live Meilisearch at MEILI_URL")
def test_live_hybrid_semantic_only_match():
    # Isolate the VECTOR half of hybrid: the on-topic doc shares NO word with the
    # query, but its vector is the closest, so hybrid must still rank it #1 - proving
    # the user-provided embedder is actually wired (not just keyword search).
    near = [0.9] + [0.0] * (meili.EMBED_DIM - 1)
    far = [0.0] * (meili.EMBED_DIM - 1) + [0.9]
    docs = [
        {"id": "sem-a", "content": "alpha bravo charlie delta", "source": "kb",
         "path": "it/a.md#0", "file": "it/a.md", "_vectors": {meili.EMBEDDER: near}},
        {"id": "sem-b", "content": "echo foxtrot golf hotel", "source": "kb",
         "path": "it/b.md#0", "file": "it/b.md", "_vectors": {meili.EMBEDDER: far}},
    ]
    try:
        meili.reindex_kb(docs)
        # The query text matches NEITHER doc's words; only the query VECTOR (== near,
        # i.e. doc a) can decide the ranking.
        hits = meili.search_hybrid(near, "zzzznonexistent qqqterm", k=5)
        assert hits and hits[0]["path"] == "it/a.md#0"
    finally:
        with meili._client() as c:
            c.delete(f"/indexes/{meili.INDEX}/documents")
