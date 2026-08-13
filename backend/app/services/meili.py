"""Meilisearch client for the LOCAL knowledge-base retrieval engine (PROMPT §14.3).

The /knowledge folder KB is indexed here and retrieved with Meilisearch HYBRID
search (BM25 keyword + vector). We embed the documents and the query OURSELVES with
the owner's Mistral endpoint (`rag._embed_raw`) and hand Meilisearch the vectors via
a `userProvided` embedder, so the embedding API key is NEVER given to Meilisearch
and query-embedding cost stays billable. CVEs (`source='cve'`) are a separate,
never-retrieved corpus and stay on pgvector/KnowledgeChunk - this module is KB-only.

Errors are raised (fail-loud): the dev-pipeline RAG block try/excepts so it degrades,
and the billable knowledge path surfaces a hard failure rather than silently
returning nothing.
"""
import logging
import time

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

INDEX = "kb"
EMBEDDER = "default"
EMBED_DIM = 1024  # mistral-embed; must match rag.EMBED_DIM
_ADD_BATCH = 100
_TASK_TIMEOUT = 30.0  # seconds to wait for an async Meili task to settle


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.meili_url.rstrip("/"),
        headers={"Authorization": f"Bearer {settings.meili_master_key}"},
        timeout=60,
    )


def _err_code(resp: httpx.Response) -> str:
    try:
        return resp.json().get("code", "")
    except Exception:
        return ""


def _wait_task(c: httpx.Client, task: dict) -> None:
    """Block until an async Meili task settles (or times out). Raises on failure."""
    uid = task.get("taskUid", task.get("uid"))
    if uid is None:
        return
    deadline = time.monotonic() + _TASK_TIMEOUT
    while time.monotonic() < deadline:
        r = c.get(f"/tasks/{uid}")
        r.raise_for_status()
        status = r.json().get("status")
        if status == "succeeded":
            return
        if status == "failed":
            raise RuntimeError(f"meili task {uid} failed: {r.json().get('error')}")
        time.sleep(0.2)
    raise TimeoutError(f"meili task {uid} did not settle within {_TASK_TIMEOUT}s")


def _enable_vector_experimental(c: httpx.Client) -> None:
    """Vector search is experimental on Meilisearch < 1.13 (feature key
    `vectorStore`) and GA afterwards (key gone). Enable it only when the running
    version still exposes the toggle, so one setup path works across versions."""
    try:
        r = c.get("/experimental-features")
        if r.status_code != 200:
            return
        feats = r.json()
        if isinstance(feats, dict) and feats.get("vectorStore") is False:
            c.patch("/experimental-features", json={"vectorStore": True}).raise_for_status()
    except Exception as exc:  # never block indexing on this best-effort toggle
        log.debug("meili vectorStore experimental toggle skipped: %s", exc)


def _configure_index(c: httpx.Client, name: str) -> None:
    """Create index `name` if absent and apply the KB settings (idempotent): keyword
    search over `content`, a userProvided embedder at EMBED_DIM. Index creation is
    async (POST returns a task that later FAILS with index_already_exists on a
    duplicate), so we only create when GET reports absence."""
    if c.get(f"/indexes/{name}").status_code != 200:
        r = c.post("/indexes", json={"uid": name, "primaryKey": "id"})
        r.raise_for_status()
        _wait_task(c, r.json())
    settings_body = {
        "searchableAttributes": ["content"],
        "filterableAttributes": ["source", "file", "tags", "content_class", "block_hash"],
        "embedders": {EMBEDDER: {"source": "userProvided", "dimensions": EMBED_DIM}},
    }
    resp = c.patch(f"/indexes/{name}/settings", json=settings_body)
    resp.raise_for_status()
    _wait_task(c, resp.json())


def reindex_kb(docs: list[dict]) -> int:
    """Atomic full-replace of the KB index. Each doc is
    {id, content, source:'kb', path, file, tags, content_class,
    _vectors:{default: embedding}}.

    Build a FRESH temp index (`kb_<ts>`), add every doc there, then atomically swap it
    into the stable `kb` name (Meilisearch `/swap-indexes`, atomic since v1.0) and drop
    the swapped-out old index. Readers of `kb` (search_hybrid / all_kb_docs /
    kb_doc_count) therefore always see the complete OLD set or the complete NEW set -
    never the empty/partial window a delete-then-add would expose to a concurrent dev
    build's RAG block, a billed search_knowledge, or the program-output leak scan.
    Returns the number of documents indexed."""
    tmp = f"{INDEX}_{int(time.time() * 1000)}"
    with _client() as c:
        _enable_vector_experimental(c)
        _configure_index(c, tmp)
        last: dict | None = None
        for i in range(0, len(docs), _ADD_BATCH):
            r = c.post(f"/indexes/{tmp}/documents", json=docs[i:i + _ADD_BATCH])
            r.raise_for_status()
            last = r.json()
        if last is not None:
            _wait_task(c, last)
        # The swap needs both operands to exist. First-ever run: create + configure an
        # empty `kb` so it also carries the embedder regardless of whether settings
        # travel with the swap; it is immediately swapped to the full temp set.
        if c.get(f"/indexes/{INDEX}").status_code != 200:
            _configure_index(c, INDEX)
        sw = c.post("/swap-indexes", json=[{"indexes": [tmp, INDEX]}])
        sw.raise_for_status()
        _wait_task(c, sw.json())
        # `tmp` now holds the OLD corpus - drop it. Best-effort: a lagging delete never
        # affects the already-swapped, complete `kb`.
        try:
            dl = c.delete(f"/indexes/{tmp}")
            dl.raise_for_status()
            _wait_task(c, dl.json())
        except Exception as exc:
            log.warning("meili: could not delete swapped-out index %s: %s", tmp, exc)
    return len(docs)


def kb_doc_count() -> int:
    """Document count of the live `kb` index (0 when it doesn't exist). Lets the beat
    self-heal: an unchanged change-fingerprint but an empty index (Meili volume reset /
    PVC recreate) must still trigger a reindex instead of skipping forever."""
    with _client() as c:
        r = c.get(f"/indexes/{INDEX}/stats")
        if r.status_code == 404 and _err_code(r) == "index_not_found":
            return 0
        r.raise_for_status()
        return int(r.json().get("numberOfDocuments", 0))


def _tag_filter(tags: list[str] | None) -> str | None:
    """§Phase 2 KB-tag filter: restrict retrieval to docs tagged with any of the
    speciality's `tags`, PLUS untagged/general docs (fail-open on shared content so
    a sovereign build keeps OCPA/CVE chunks but drops the aws-only distractors).
    None/empty => no filter. Tags come from specialities.json (trusted); quotes are
    stripped defensively so the filter string can't be broken."""
    clean = [str(t).replace('"', "") for t in (tags or []) if str(t).strip()]
    if not clean:
        return None
    quoted = ", ".join(f'"{t}"' for t in clean)
    return f"(tags IN [{quoted}]) OR (tags IS EMPTY) OR (tags IS NULL)"


def search_hybrid(query_vec: list[float], query_text: str, k: int,
                  tags: list[str] | None = None,
                  content_class: str | None = None) -> list[dict]:
    """Hybrid (keyword + vector) search over the KB. Returns hit dicts carrying
    content/source/path/file/content_class/block_hash plus `score` (Meili's 0..1
    `_rankingScore`, or None if the running Meili omits it) so the caller can apply
    the anti-extraction retrieval floor (rag._above_threshold). `tags` (§Phase 2)
    scopes retrieval to a speciality's KB tags (+ untagged docs); a tag filter Meili
    rejects (older version) fails OPEN to unfiltered search. `content_class` (§KB
    tiers) restricts hits to one tier; unlike tags it fails CLOSED - a rejected or
    unfilterable class query returns [] rather than injecting other-tier content
    where the caller promised procedures. An index that doesn't exist yet (no
    ingest) returns []; other errors raise."""
    body = {
        "q": query_text,
        "vector": query_vec,
        "hybrid": {"embedder": EMBEDDER, "semanticRatio": 0.5},
        "limit": k,
        "showRankingScore": True,
    }
    clauses = []
    flt = _tag_filter(tags)
    if flt:
        clauses.append(f"({flt})")
    if content_class:
        clauses.append(f'content_class = "{str(content_class).replace(chr(34), "")}"')
    if clauses:
        body["filter"] = " AND ".join(clauses)
    with _client() as c:
        r = c.post(f"/indexes/{INDEX}/search", json=body)
        if r.status_code == 404 and _err_code(r) == "index_not_found":
            return []
        if clauses and r.status_code >= 400:
            if content_class:
                log.warning("meili: content_class filter rejected (%s); failing closed",
                            _err_code(r))
                return []
            log.warning("meili: tag filter rejected (%s); retrying unfiltered", _err_code(r))
            body.pop("filter", None)
            r = c.post(f"/indexes/{INDEX}/search", json=body)
            if r.status_code == 404 and _err_code(r) == "index_not_found":
                return []
        r.raise_for_status()
        hits = r.json().get("hits", [])
    return [{"content": h.get("content", ""), "source": h.get("source", "kb"),
             "path": h.get("path", ""), "file": h.get("file") or h.get("path", ""),
             "content_class": h.get("content_class") or "fact",
             "block_hash": h.get("block_hash"),
             "score": h.get("_rankingScore")}
            for h in hits]


def all_kb_docs():
    """Yield the `content` of every KB document (for the leak-scan fingerprinter and
    the kb-audit topic seeder, which used to read KnowledgeChunk directly)."""
    for doc in iter_kb_docs("content"):
        yield doc.get("content", "")


def iter_kb_docs(fields: str):
    """Paginate every KB document, yielding dicts with the requested comma-separated
    fields (§KB tiers admin: the derived-split view groups chunks back into blocks
    client-side - the owner corpus is small, so a full scan beats teaching Meili a
    prefix filter). A missing index yields nothing."""
    limit = 1000
    offset = 0
    with _client() as c:
        while True:
            r = c.get(f"/indexes/{INDEX}/documents",
                      params={"fields": fields, "limit": limit, "offset": offset})
            if r.status_code == 404 and _err_code(r) == "index_not_found":
                return
            r.raise_for_status()
            results = r.json().get("results", [])
            yield from results
            if len(results) < limit:
                return
            offset += limit
