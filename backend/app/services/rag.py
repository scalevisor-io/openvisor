"""Knowledge retrieval (PROMPT §14.3).

The local /knowledge folder KB is indexed into Meilisearch and retrieved with
HYBRID search (BM25 keyword + vector) - see services/meili.py. We embed both the
documents and the query ourselves via the owner's embedding endpoint (so query
embedding stays a billable model call) and hand Meilisearch the vectors through a
userProvided embedder; Meili's hybrid ranker replaces the old LLM reranker for KB.

CVEs (`source='cve'`, ingested for threat data, never retrieved today) remain in
the pgvector KnowledgeChunk store with the optional LLM reranker - `search`/
`retrieve` route source='cve' down that legacy path, source in (None,'kb') to Meili.
"""
import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SyncSession
from app.models import KnowledgeBase, KnowledgeChunk
from app.services import meili

log = logging.getLogger(__name__)

# The local KB lives in the gitignored `./knowledge` folder, bind-mounted read-only
# at this fixed in-container path (compose worker/beat). A constant (not settings) so
# tests can monkeypatch it to a tmp dir; K8s mounts an empty dir here today.
KNOWLEDGE_ROOT = Path("/knowledge")

EMBED_DIM = 1024  # mistral-embed

# Text-ish files worth embedding from the knowledge repo.
KB_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".yaml", ".yml", ".toml", ".json",
    ".py", ".js", ".ts", ".tsx", ".sh", ".tf", ".hcl", ".sql",
    ".cfg", ".ini", ".mdx",
}
KB_EXTRA_NAMES = {"Dockerfile", "Makefile", ".env.example"}
KB_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".idea"}
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200
MAX_FILE_BYTES = 512_000


@dataclass
class KBHit:
    """A Meilisearch KB hit shaped like the KnowledgeChunk attributes every caller
    reads (`.content`, `.source`, `.path`, `.meta`, `.file`), so retrieval callers
    are engine-agnostic."""
    content: str
    source: str = "kb"
    path: str = ""
    file: str = ""
    meta: dict = field(default_factory=dict)
    score: float | None = None  # Meili hybrid ranking score (0..1), None if unavailable
    content_class: str = "fact"  # §KB tiers: fact | rule | procedure (pre-tier docs: fact)


def _embed_raw(texts: list[str]) -> tuple[list[list[float]], dict]:
    """Embed and also return the model usage (so metered callers can bill it).
    usage = {model, input_tokens, output_tokens} like llm.chat()."""
    resp = httpx.post(
        f"{settings.embedding_base_url.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
        json={"model": settings.embedding_model, "input": texts},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    usage = {
        "model": data.get("model", settings.embedding_model),
        "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
        "output_tokens": 0,
    }
    return [d["embedding"] for d in data["data"]], usage


def embed(texts: list[str]) -> list[list[float]]:
    return _embed_raw(texts)[0]


def local_kb_enabled(db: Session) -> bool:
    """Whether the built-in local KB (§KB) is enabled - gates whether the local
    /knowledge root is folded into the reindex. Cheap single-row read; a missing row
    (before the KB is seeded) is treated as enabled for backward-compatibility."""
    row = db.execute(select(KnowledgeBase).where(
        KnowledgeBase.kind == "local")).scalar_one_or_none()
    return True if row is None else bool(row.enabled)


def _any_git_kb_active(db: Session) -> bool:
    """Whether at least one git knowledge source is enabled AND verified (its docs
    live in the same Meili `kb` index as the local KB)."""
    return db.execute(select(KnowledgeBase.id).where(
        KnowledgeBase.kind == "git",
        KnowledgeBase.enabled.is_(True),
        KnowledgeBase.verified.is_(True)).limit(1)).first() is not None


def kb_retrieval_enabled(db: Session) -> bool:
    """Whether the shared `kb` index is worth querying: the local KB is enabled OR a
    git source is live. Retrieval short-circuits to []/([], []) WITHOUT embedding the
    query (costs nothing) only when no source is active - so a git-only KB (local
    disabled but a git source connected) still retrieves."""
    return local_kb_enabled(db) or _any_git_kb_active(db)


def selected_root_keys(db: Session, kb_ids: list | None) -> set[str] | None:
    """Map a project's KB selection (Project.kb_ids: null = all, [] = none, a list =
    exactly those KnowledgeBase ids) to the Meili doc namespaces it may read: the
    literal 'local' root for the local KB row, each git source's row id. Effective
    access = globally enabled AND verified AND selected - selection only narrows, so
    the /admin/knowledge-bases kill-switch always cascades. None = unrestricted."""
    if kb_ids is None:
        return None
    ids = set(kb_ids)
    roots: set[str] = set()
    local = db.execute(select(KnowledgeBase).where(
        KnowledgeBase.kind == "local")).scalar_one_or_none()
    if local is not None and local.enabled and local.id in ids:
        roots.add("local")
    git_ids = db.execute(select(KnowledgeBase.id).where(
        KnowledgeBase.kind == "git",
        KnowledgeBase.enabled.is_(True),
        KnowledgeBase.verified.is_(True))).scalars().all()
    roots.update(i for i in git_ids if i in ids)
    return roots


def _root_filter(hits: list, roots: set[str] | None) -> list:
    """Keep only hits whose doc namespace prefix ('<root_key>/...' on `file`/`path`,
    §KB ingest) is in the allowed set. Fail-closed: a hit with no provenance is
    dropped when a selection is active."""
    if roots is None:
        return hits
    return [h for h in hits if (h.file or h.path).split("/", 1)[0] in roots]


def search(db: Session, query: str, source: str | None = None, k: int = 8,
           rerank: bool = True, tags: list[str] | None = None,
           kb_ids: list | None = None) -> list:
    """Retrieve KB passages (Meilisearch hybrid). source='cve' takes the legacy
    pgvector + optional-LLM-rerank path (nothing routes there today). `tags` (§Phase 2)
    scopes retrieval to a speciality's KB tags (+ untagged docs). `kb_ids` is a
    project's KB selection (null = all): the shared index is queried then hits are
    root-filtered to the selected sources; a selection matching no active source
    returns [] without embedding, like a disabled local KB."""
    if source == "cve":
        return _cve_search(db, query, k, rerank)
    roots = selected_root_keys(db, kb_ids)
    if roots is not None and not roots:
        return []
    if not kb_retrieval_enabled(db):
        return []
    vec, _usage = _embed_raw([query])
    fetch_k = k if roots is None else k * 3
    hits = _above_threshold(
        [_hit(h) for h in meili.search_hybrid(vec[0], query, fetch_k, tags=tags)])
    return _root_filter(hits, roots)[:k]


def retrieve(db: Session, query: str, k: int = 6,
             source: str | None = "kb", tags: list[str] | None = None,
             kb_ids: list | None = None) -> tuple[list, list[dict]]:
    """Hybrid KB retrieval returning the hits AND the model usages incurred (the
    query embedding) so the caller can meter them. source='cve' keeps the legacy
    pgvector + rerank path. Defaults to the 'kb' source (the owner's knowledge base).
    A disabled local KB - or a `kb_ids` selection matching no active source - returns
    ([], []) without embedding the query (costs nothing)."""
    if source == "cve":
        vec, emb_usage = _embed_raw([query])
        hits, rr_usage = _cve_retrieve(db, vec[0], query, k)
        usages = [emb_usage]
        if rr_usage:
            usages.append(rr_usage)
        return hits, usages
    roots = selected_root_keys(db, kb_ids)
    if roots is not None and not roots:
        return [], []
    if not kb_retrieval_enabled(db):
        return [], []
    vec, emb_usage = _embed_raw([query])
    fetch_k = k if roots is None else k * 3
    hits = _above_threshold(
        [_hit(h) for h in meili.search_hybrid(vec[0], query, fetch_k, tags=tags)])
    return _root_filter(hits, roots)[:k], [emb_usage]


def _hit(h: dict) -> KBHit:
    file = h.get("file") or h.get("path", "")
    return KBHit(content=h.get("content", ""), source=h.get("source", "kb"),
                 path=h.get("path", ""), file=file,
                 meta={"file": file, "block_hash": h.get("block_hash")},
                 score=h.get("score"),
                 content_class=h.get("content_class") or "fact")


def _above_threshold(hits: list) -> list:
    """Anti-extraction retrieval floor (§KB hardening): drop hits whose hybrid ranking
    score is below settings.kb_retrieval_min_score. Top-k always returns the least-bad
    matches, so an off-topic corpus-sweep query (the RAG knowledge-extraction vector)
    otherwise leaks a chunk at a time; the floor makes such a query return nothing. A hit
    with no score (older Meili, a monkeypatched test stub) is KEPT - the floor only ever
    removes a scored, low-relevance chunk, so it can never silently empty a legitimate
    result set on infra that omits scores. 0 disables it."""
    floor = settings.kb_retrieval_min_score
    if floor <= 0:
        return hits
    return [h for h in hits if h.score is None or h.score >= floor]


# ---- legacy pgvector path (CVE corpus only) ----

def _cve_search(db: Session, query: str, k: int, rerank: bool) -> list[KnowledgeChunk]:
    vec = embed([query])[0]
    fetch = k * 3 if rerank else k
    q = (select(KnowledgeChunk).where(KnowledgeChunk.source == "cve")
         .order_by(KnowledgeChunk.embedding.cosine_distance(vec)).limit(fetch))
    hits = list(db.execute(q).scalars().all())
    if rerank and len(hits) > k:
        hits = _rerank(query, hits)[0][:k]
    return hits[:k]


def _cve_retrieve(db: Session, vec: list[float], query: str,
                  k: int) -> tuple[list[KnowledgeChunk], dict | None]:
    q = (select(KnowledgeChunk).where(KnowledgeChunk.source == "cve")
         .order_by(KnowledgeChunk.embedding.cosine_distance(vec)).limit(k * 3))
    hits = list(db.execute(q).scalars().all())
    rr_usage = None
    if len(hits) > k:
        hits, rr_usage = _rerank(query, hits)
    return hits[:k], rr_usage


def _rerank(query: str, chunks: list[KnowledgeChunk]) -> tuple[list[KnowledgeChunk], dict | None]:
    """LLM-as-reranker for the pgvector CVE path: the configured reranker endpoint is
    an OpenAI-compatible chat API (no dedicated /rerank at Mistral), so a small model
    scores each candidate 0-10 for relevance. Returns (ordered_chunks, usage|None) -
    usage is None when reranking is disabled or falls back to the vector order on error."""
    import json

    if not settings.reranker_model or settings.reranker_model == "none":
        return chunks, None
    numbered = "\n\n".join(f"[{i}] {c.content[:600]}" for i, c in enumerate(chunks))
    try:
        resp = httpx.post(
            f"{settings.reranker_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.reranker_api_key}"},
            json={
                "model": settings.reranker_model,
                "max_tokens": 512,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content":
                        "Score each numbered passage 0-10 for relevance to the query. "
                        'Respond with ONLY JSON: {"scores": {"0": <n>, "1": <n>, ...}}'},
                    {"role": "user", "content": f"Query: {query}\n\nPassages:\n{numbered}"},
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        scores = json.loads(data["choices"][0]["message"]["content"])["scores"]
        usage = {
            "model": data.get("model", settings.reranker_model),
            "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
        }
        ordered = sorted(chunks, key=lambda c: -float(scores.get(str(chunks.index(c)), 0)))
        return ordered, usage
    except Exception as exc:
        log.warning("rerank failed, keeping vector order: %s", exc)
        return chunks, None


def _chunk_text(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        out.append(text[start:start + CHUNK_CHARS])
        start += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def _kb_files(root: Path):
    # A git knowledge source is untrusted third-party content, so never follow
    # symlinks out of the root: a committed symlink to /etc/… or another clone would
    # otherwise be indexed into the shared KB. Skip any symlink (leaf), and require
    # the resolved real path to stay inside the root (catches a symlinked parent dir
    # too). Applied to the local root as well (defence in depth).
    root_real = os.path.realpath(root)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        real = os.path.realpath(path)
        if real != root_real and not real.startswith(root_real + os.sep):
            continue
        if any(part in KB_SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in KB_EXTENSIONS or path.name in KB_EXTRA_NAMES:
            try:
                if path.stat().st_size <= MAX_FILE_BYTES:
                    yield path
            except OSError:
                continue


def _default_roots() -> list[tuple[str, Path]]:
    """The single local root, used when no explicit root list is supplied (tests /
    a caller that only wants /knowledge)."""
    return [("local", KNOWLEDGE_ROOT)]


def _root_fingerprint(root: Path) -> str:
    """file count + total size + newest mtime over a root's embeddable files."""
    if not root.is_dir():
        return "absent"
    count = 0
    total = 0
    newest = 0.0
    for path in _kb_files(root):
        try:
            st = path.stat()
        except OSError:
            continue
        count += 1
        total += st.st_size
        newest = max(newest, st.st_mtime)
    return f"{count}:{total}:{newest:.0f}" if count else "empty"


def kb_tree_fingerprint(roots: list[tuple[str, Path]] | None = None) -> str:
    """Cheap change signal folded over every root (local /knowledge + each cloned
    git-KB checkout): per-root file count + total size + newest mtime. The beat
    compares it against the last-indexed fingerprint and only re-embeds when it
    changed, so the daily job is a no-op unless a tree actually moved. Because the
    git checkouts are refreshed (fetch + reset --hard) before this runs, a moved HEAD
    rewrites the changed files' mtimes and so changes the fingerprint. Parts are
    SORTED before joining - the same roots must always produce the same string
    whatever order the caller enumerated them in (a SELECT without ORDER BY shuffles
    as rows are rewritten, and an order-sensitive join re-embeds the whole corpus on
    every tick). No roots (all disabled) -> a stable 'empty' sentinel."""
    if roots is None:
        roots = _default_roots()
    parts = sorted(f"{key}={_root_fingerprint(root)}" for key, root in roots)
    return ";".join(parts) if parts else "empty"


def ingest_knowledge_repo(roots: list[tuple[str, Path]] | None = None,
                          had_source_errors: bool = False) -> int:
    """Embed every knowledge root and index them into Meilisearch as source='kb' in a
    SINGLE atomic reindex. Each root is (root_key, path): the local /knowledge folder
    (root_key 'local') plus each enabled+verified git source (root_key = kb id, its
    checkout dir). Every doc is namespaced by root_key - id=blake2b('<key>/<rel>#<idx>'),
    path='<key>/<rel>#<idx>', file='<key>/<rel>' - so two files sharing a name across
    roots never collide/overwrite. Idempotent: replaces the whole 'kb' index each run
    (reindex_kb called ONCE with the union). CVEs live in pgvector, not here.

    §KB tiers: every markdown file is segmented into blocks and each block classified
    fact|rule|procedure (services/kb_classify - deterministic signals, then the cached/
    LLM classifier); chunk windows reset at block boundaries so a chunk never straddles
    two classes, and each doc carries its block's `content_class`. Rule blocks
    additionally compile into per-root standing-rules digests (budget-bounded; overflow
    demotes the block to procedure), persisted to kb_rules_digest AFTER the index swap
    succeeds so digest and index always describe the same corpus generation.

    Degraded-classification guard: when any file's classification fell back to fact
    without a real verdict (classifier unavailable, or blocks past the per-call cap -
    classify_file's degraded flag), the retrieval index is still swapped (facts still
    retrieve; the tiers are additive) but the digest and procedure registries KEEP
    their previous generation - fallback verdicts must never disarm standing rules.

    Wipe-guard: `reindex_kb` full-replaces the live index, so when `had_source_errors`
    is set (a source failed to sync this run) AND zero docs were gathered, the reindex
    is SKIPPED (returns -1) - a transient clone failure on the only active source must
    keep the previously-indexed content (and the digests) rather than swap the index to
    empty. A genuinely empty KB (no docs, no errors) still reindexes to empty."""
    from app.services import kb_classify

    if roots is None:
        roots = _default_roots()

    # (root_key, rel, tags, block_text, class, block_hash) in file order per root.
    blocks: list[tuple[str, str, list, str, str, str]] = []
    degraded = False
    for root_key, root in roots:
        if not root.is_dir():
            log.warning("knowledge root %s (%s) not present; skipping", root, root_key)
            continue
        for path in _kb_files(root):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel = str(path.relative_to(root))
            # §Phase 2 KB tag: the doc's top-level folder is its tag, so a KB organised
            # by speciality (/knowledge/aws-well-architected/…, /kubernetes-hardening/…) can be
            # tag-filtered at retrieval. A root-level file is untagged (general/shared).
            tags = [rel.split("/", 1)[0]] if "/" in rel else []
            triples, file_degraded = kb_classify.classify_file(rel, text)
            degraded = degraded or file_degraded
            for block_text, cls, block_hash in triples:
                blocks.append((root_key, rel, tags, block_text, cls, block_hash))

    if not blocks and had_source_errors:
        log.warning("ingest_knowledge: every active source errored and yielded 0 docs; "
                    "keeping the existing index instead of swapping it to empty")
        return -1

    rule_blocks: dict[str, list[tuple[str, str, str]]] = {}
    for root_key, rel, _tags, block_text, cls, block_hash in blocks:
        if cls == "rule":
            rule_blocks.setdefault(root_key, []).append((rel, block_text, block_hash))
    digests, demoted = kb_classify.compile_digests(rule_blocks)

    # (path#idx, content, tags, class, block_hash)
    records: list[tuple[str, str, list, str, str]] = []
    chunk_idx: dict[str, int] = {}  # running chunk index per '<root_key>/<rel>'
    procedures: list[tuple[str, str, str, str]] = []  # (root_key, rel, text, hash)
    for root_key, rel, tags, block_text, cls, block_hash in blocks:
        if cls == "rule" and block_hash in demoted:
            cls = "procedure"
        if cls == "procedure":
            procedures.append((root_key, rel, block_text, block_hash))
        file_key = f"{root_key}/{rel}"
        for chunk in _chunk_text(block_text):
            i = chunk_idx.get(file_key, 0)
            records.append((f"{file_key}#{i}", chunk, tags, cls, block_hash))
            chunk_idx[file_key] = i + 1

    docs: list[dict] = []
    for batch_start in range(0, len(records), 32):
        batch = records[batch_start:batch_start + 32]
        vectors = embed([c for _, c, _, _, _ in batch])
        for (rec_path, content, tags, cls, block_hash), vec in zip(batch, vectors):
            file = rec_path.split("#")[0]
            docs.append({
                "id": hashlib.blake2b(rec_path.encode(), digest_size=16).hexdigest(),
                "content": content,
                "source": "kb",
                "path": rec_path,
                "file": file,
                "tags": tags,
                "content_class": cls,
                "block_hash": block_hash,
                "_vectors": {meili.EMBEDDER: vec},
            })
    n = meili.reindex_kb(docs)
    if degraded:
        log.warning("ingest_knowledge: classification degraded this run (classifier "
                    "unavailable or block cap exceeded); keeping the previous "
                    "standing-rules digests and procedures")
    else:
        _store_digests(digests)
        _store_procedures(procedures)
    log.info("ingested %d knowledge chunks from %d root(s) into Meilisearch "
             "(%d standing-rules digest(s), %d procedure(s))",
             n, len(roots), len(digests), len(procedures))
    return n


def _store_digests(digests: dict[str, str]) -> None:
    """Replace the kb_rules_digest rows as a set, mirroring the atomic index swap the
    caller just performed: a root with no rule blocks this generation loses its row,
    so a disabled/emptied source stops injecting rules at the next reindex. The
    delete MUST be a Core statement (emitted immediately), not ORM db.delete(): the
    unit of work flushes INSERTs before DELETEs on the same table, so re-inserting a
    root_key that still has a row would trip its unique constraint."""
    from app.models import KbRulesDigest
    with SyncSession() as db:
        db.execute(delete(KbRulesDigest))
        for root_key, content in digests.items():
            db.add(KbRulesDigest(root_key=root_key, content=content,
                                 char_count=len(content)))
        db.commit()


def _store_procedures(procedures: list[tuple[str, str, str, str]]) -> None:
    """Replace the kb_procedure rows as a set ((root_key, rel, text, hash) tuples),
    mirroring the atomic index swap like _store_digests. Title = the block's first
    heading line, else the file name."""
    from app.models import KbProcedure
    with SyncSession() as db:
        db.execute(delete(KbProcedure))
        for root_key, rel, text, block_hash in procedures:
            first = text.strip().splitlines()[0] if text.strip() else ""
            title = (first.lstrip("#").strip() if first.startswith("#")
                     else Path(rel).stem)[:255]
            db.add(KbProcedure(root_key=root_key, rel=rel, title=title or rel,
                               content=text, content_hash=block_hash))
        db.commit()


def _enabled_source_names(db: Session) -> dict[str, str]:
    """root_key -> display name over ENABLED sources only (verified too, for git) -
    the shared kill-switch map for digest and procedure injection: content of a
    source disabled since the last reindex drops out here, ahead of the reindex
    that deletes its rows."""
    names: dict[str, str] = {}
    if local_kb_enabled(db):
        local = db.execute(select(KnowledgeBase).where(
            KnowledgeBase.kind == "local")).scalar_one_or_none()
        names["local"] = local.name if local else "Local knowledge"
    for kb in db.execute(select(KnowledgeBase).where(
            KnowledgeBase.kind == "git",
            KnowledgeBase.enabled.is_(True),
            KnowledgeBase.verified.is_(True))).scalars().all():
        names[kb.id] = kb.name
    return names


def rules_digests(db: Session, kb_ids: list | None) -> list[tuple[str, str]]:
    """(source name, digest text) for every standing-rules digest the project may
    see, narrowed by its kb_ids selection exactly like retrieval: None = every
    digest, [] = none, a list = those sources' roots only. Names come from
    _enabled_source_names, so the /admin/knowledge-bases kill-switch stops digest
    injection immediately."""
    from app.models import KbRulesDigest
    allowed = selected_root_keys(db, kb_ids)
    if allowed is not None and not allowed:
        return []
    rows = db.execute(select(KbRulesDigest)).scalars().all()
    if allowed is not None:
        rows = [r for r in rows if r.root_key in allowed]
    if not rows:
        return []
    names = _enabled_source_names(db)
    return [(names[r.root_key], r.content)
            for r in sorted(rows, key=lambda r: r.root_key) if r.root_key in names]


def procedures_for(db: Session, query: str, kb_ids: list | None,
                   k: int | None = None) -> list[tuple[str, str, str]]:
    """§KB tiers: the procedures whose trigger matches this task - hybrid retrieval
    over the procedure-class docs (fail-closed class filter, retrieval floor, kb_ids
    root narrowing), hits mapped back to kb_procedure through `block_hash` so the
    run receives FULL bodies, never chunk windows. Returns up to k (source name,
    title, content) triples, deduped by block. Same enabled-source kill-switch as
    rules_digests; empty query or no active source returns [] without embedding."""
    from app.models import KbProcedure
    k = k or settings.kb_procedures_k
    if not (query or "").strip():
        return []
    roots = selected_root_keys(db, kb_ids)
    if roots is not None and not roots:
        return []
    if not kb_retrieval_enabled(db):
        return []
    vec, _usage = _embed_raw([query])
    fetch_k = k * 3
    hits = _above_threshold(
        [_hit(h) for h in meili.search_hybrid(vec[0], query, fetch_k,
                                              content_class="procedure")])
    hits = _root_filter(hits, roots)
    hashes: list[str] = []
    for h in hits:
        bh = (h.meta or {}).get("block_hash")
        if bh and bh not in hashes:
            hashes.append(bh)
    if not hashes:
        return []
    names = _enabled_source_names(db)
    rows = {r.content_hash: r for r in db.execute(select(KbProcedure).where(
        KbProcedure.content_hash.in_(hashes))).scalars().all()
        if roots is None or r.root_key in roots}
    out: list[tuple[str, str, str]] = []
    for bh in hashes:
        row = rows.get(bh)
        if row is None or row.root_key not in names:
            continue
        out.append((names[row.root_key], row.title, row.content))
        if len(out) >= k:
            break
    return out


def ingest_recent_cves() -> int:
    """Pull recent CVEs from the NVD API and embed them into pgvector. Returns count."""
    resp = httpx.get(
        "https://services.nvd.nist.gov/rest/json/cves/2.0",
        params={"resultsPerPage": 100, "startIndex": 0},
        timeout=60,
    )
    resp.raise_for_status()
    vulns = resp.json().get("vulnerabilities", [])
    if not vulns:
        return 0
    chunks = []
    for v in vulns:
        cve = v.get("cve", {})
        cve_id = cve.get("id", "?")
        descs = [d["value"] for d in cve.get("descriptions", []) if d.get("lang") == "en"]
        if descs:
            chunks.append((cve_id, f"{cve_id}: {descs[0][:2000]}"))
    n = 0
    with SyncSession() as db:
        existing = {p for (p,) in db.execute(
            select(KnowledgeChunk.path).where(KnowledgeChunk.source == "cve")).all()}
        fresh = [(cid, content) for cid, content in chunks if cid not in existing]
        for batch_start in range(0, len(fresh), 32):
            batch = fresh[batch_start:batch_start + 32]
            vectors = embed([c for _, c in batch])
            for (cid, content), vec in zip(batch, vectors):
                db.add(KnowledgeChunk(source="cve", path=cid, content=content,
                                      embedding=vec, meta={"cve_id": cid}))
                n += 1
        db.commit()
    return n
