"""§KB tiers: block-level classification of blended knowledge documents, the
standing-rules digest compilation, and its injection into the dev task."""
import uuid

import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.core.db import SyncSession
from app.models import KbBlockClass, KbProcedure, KbRulesDigest, KnowledgeBase
from app.services import kb_classify


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SyncSession() as db:
        db.execute(delete(KbBlockClass))
        db.execute(delete(KbRulesDigest))
        db.execute(delete(KbProcedure))
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.kind == "git"))
        db.commit()


def _no_llm(blocks):
    raise AssertionError("classifier LLM must not be called here")


# ------------------------------------------------------------- segmentation

def test_split_blocks_headings_and_preamble():
    body = "intro paragraph\n\n# One\ntext one\n\n## Two\ntext two"
    blocks = kb_classify.split_blocks(body)
    assert blocks == ["intro paragraph", "# One\ntext one", "## Two\ntext two"]


def test_split_blocks_resplits_oversized_sections():
    paras = [f"paragraph {i} " + "x" * 500 for i in range(6)]
    body = "# Big\n" + "\n\n".join(paras)
    blocks = kb_classify.split_blocks(body)
    assert len(blocks) > 1
    # every paragraph survives in exactly one block
    joined = "\n\n".join(blocks)
    for p in paras:
        assert p in joined
    assert all(len(b) <= kb_classify.MAX_BLOCK_CHARS + 10 for b in blocks)


def test_parse_frontmatter_valid_and_malformed():
    meta, body = kb_classify.parse_frontmatter("---\ntype: Rule\ntags: [a]\n---\nBody.")
    assert meta["type"] == "Rule" and body == "Body."
    meta2, body2 = kb_classify.parse_frontmatter("---\n: bad: [yaml\n---\nBody.")
    assert meta2 == {} and body2 == "Body."
    meta3, body3 = kb_classify.parse_frontmatter("no frontmatter here")
    assert meta3 == {} and body3 == "no frontmatter here"


# ------------------------------------------------------------- deterministic signals

def test_file_signal_precedence_and_conventions():
    assert kb_classify.file_signal("rules/x.md", {"type": "Rule"}) == "rule"
    assert kb_classify.file_signal("x.md", {"type": "Principle"}) == "rule"
    assert kb_classify.file_signal("x.md", {"type": "Runbook"}) == "procedure"
    # frontmatter beats the path convention
    assert kb_classify.file_signal("skills/x.md", {"type": "Rule"}) == "rule"
    assert kb_classify.file_signal("AGENTS.md", {}) == "rule"
    assert kb_classify.file_signal("sub/CLAUDE.md", {}) == "rule"
    assert kb_classify.file_signal("skills/deploy.md", {}) == "procedure"
    assert kb_classify.file_signal("any/SKILL.md", {}) == "procedure"
    assert kb_classify.file_signal("guide.md", {"type": "Note"}) is None
    assert kb_classify.file_signal("guide.md", {}) is None


# ------------------------------------------------------------- classify_file

def test_classify_file_non_markdown_is_fact(monkeypatch):
    monkeypatch.setattr(kb_classify, "_llm_classify", _no_llm)
    out, degraded = kb_classify.classify_file("main.py", "print('x')")
    assert len(out) == 1 and out[0][1] == "fact"
    assert degraded is False


def test_classify_file_signal_covers_all_blocks_without_llm(monkeypatch):
    monkeypatch.setattr(kb_classify, "_llm_classify", _no_llm)
    text = "---\ntype: Rule\n---\nIntro.\n\n# A\nalways do a\n\n# B\nnever do b"
    out, degraded = kb_classify.classify_file("rules/conv.md", text)
    # frontmatter fact block + three rule body blocks
    assert [cls for _t, cls, _h in out] == ["fact", "rule", "rule", "rule"]
    assert out[0][0].startswith("---")
    assert degraded is False


def test_classify_file_llm_verdicts_apply_and_cache(monkeypatch):
    calls = {"n": 0}

    def fake_llm(blocks):
        calls["n"] += 1
        return ["fact", "rule"][:len(blocks)]
    monkeypatch.setattr(kb_classify, "_llm_classify", fake_llm)
    text = "The company context.\n\n# Conventions\nevery commit must cite its issue"
    out, degraded = kb_classify.classify_file("README.md", text)
    assert [cls for _t, cls, _h in out] == ["fact", "rule"]
    assert calls["n"] == 1 and degraded is False
    # second ingest of the same content: cache hits, no LLM call
    out2, degraded2 = kb_classify.classify_file("README.md", text)
    assert [cls for _t, cls, _h in out2] == ["fact", "rule"]
    assert calls["n"] == 1 and degraded2 is False


def test_classify_file_llm_failure_defaults_to_fact_and_is_not_cached(monkeypatch):
    calls = {"n": 0}

    def failing_llm(blocks):
        calls["n"] += 1
        return None
    monkeypatch.setattr(kb_classify, "_llm_classify", failing_llm)
    text = "Some text.\n\n# More\nother text"
    out, degraded = kb_classify.classify_file("README.md", text)
    assert all(cls == "fact" for _t, cls, _h in out)
    assert degraded is True
    # a fallback verdict is per-run: the next ingest retries the classifier
    kb_classify.classify_file("README.md", text)
    assert calls["n"] == 2


def test_classify_file_block_cap_overflow_flags_degraded(monkeypatch):
    """Blocks past the per-call cap default to fact UNCACHED - that is a degraded
    classification too, so a very large file never silently freezes as all-fact."""
    asked = {}

    def fake_llm(blocks):
        asked["n"] = len(blocks)
        return ["fact"] * len(blocks)
    monkeypatch.setattr(kb_classify, "_llm_classify", fake_llm)
    text = "\n\n".join(f"# H{i}\nblock {i}"
                      for i in range(kb_classify._LLM_MAX_BLOCKS + 3))
    out, degraded = kb_classify.classify_file("book.md", text)
    assert asked["n"] == kb_classify._LLM_MAX_BLOCKS
    assert len(out) == kb_classify._LLM_MAX_BLOCKS + 3
    assert degraded is True


def test_admin_override_beats_file_signal(monkeypatch):
    monkeypatch.setattr(kb_classify, "_llm_classify", _no_llm)
    text = "# A\nalways do a\n\n# B\nactually just context"
    b_hash = kb_classify.norm_hash("# B\nactually just context")
    with SyncSession() as db:
        db.add(KbBlockClass(content_hash=b_hash, content_class="fact", origin="override"))
        db.commit()
    out, _degraded = kb_classify.classify_file("AGENTS.md", text)
    assert [cls for _t, cls, _h in out] == ["rule", "fact"]


# ------------------------------------------------------------- digest compilation

def test_compile_digests_groups_by_file_and_demotes_overflow(monkeypatch):
    monkeypatch.setattr(settings, "kb_rules_digest_max_chars", 60)
    blocks = {"local": [
        ("a.md", "rule one text", "h1"),
        ("a.md", "rule two text", "h2"),
        ("b.md", "x" * 200, "h3"),  # never fits the 60-char budget
    ]}
    digests, demoted = kb_classify.compile_digests(blocks)
    assert "### a.md" in digests["local"]
    assert "rule one text" in digests["local"] and "rule two text" in digests["local"]
    assert demoted == {"h3"}


# ------------------------------------------------------------- ingest integration

def test_ingest_classifies_chunks_and_stores_digests(monkeypatch, tmp_path):
    from app.services import meili, rag

    (tmp_path / "rules.md").write_text("---\ntype: Rule\n---\nAlways pin versions.")
    (tmp_path / "context.md").write_text("The customer builds C2 software.")
    # real (healthy) fact verdicts - a None fallback would flag the run degraded
    monkeypatch.setattr(kb_classify, "_llm_classify",
                        lambda blocks: ["fact"] * len(blocks))
    monkeypatch.setattr(rag, "embed", lambda texts: [[0.1] * rag.EMBED_DIM for _ in texts])
    captured = {}

    def _reindex(docs):
        captured["docs"] = docs
        return len(docs)
    monkeypatch.setattr(meili, "reindex_kb", _reindex)

    rag.ingest_knowledge_repo([("local", tmp_path)])
    by_file = {}
    for d in captured["docs"]:
        by_file.setdefault(d["file"], []).append(d)
    classes = {d["content"]: d["content_class"] for d in by_file["local/rules.md"]}
    assert classes["Always pin versions."] == "rule"
    assert all(d["content_class"] == "fact" for d in by_file["local/context.md"])
    with SyncSession() as db:
        from sqlalchemy import select
        row = db.execute(select(KbRulesDigest).where(
            KbRulesDigest.root_key == "local")).scalar_one()
        assert "Always pin versions." in row.content
        assert "### rules.md" in row.content


def test_store_digests_replaces_existing_roots():
    """Regression (prod regression): replacing the digest set while a root_key
    already had a row tripped the unique constraint - the ORM flushes INSERTs before
    DELETEs on the same table, so the delete must be a Core statement emitted first.
    The failure looped: every 5-minute beat tick fully re-embedded the corpus, then
    died storing the digests, and a newly added git KB's standing rules never
    landed."""
    from app.services import rag

    with SyncSession() as db:
        db.add(KbRulesDigest(root_key="local", content="old local rules",
                             char_count=15))
        db.add(KbRulesDigest(root_key="removed-root", content="stale", char_count=5))
        db.commit()

    rag._store_digests({"local": "new local rules", "book-kb": "book rules"})

    with SyncSession() as db:
        from sqlalchemy import select
        rows = {r.root_key: r.content
                for r in db.execute(select(KbRulesDigest)).scalars().all()}
    assert rows == {"local": "new local rules", "book-kb": "book rules"}


def test_degraded_classification_keeps_previous_digests_and_procedures(
        monkeypatch, tmp_path):
    """A run whose classifier fell back (LLM unavailable) must not replace the
    digest/procedure registries: fallback fact verdicts would silently disarm the
    standing rules until the next tree change. The retrieval index still swaps -
    facts still retrieve, the tiers are additive."""
    from app.services import meili, rag

    with SyncSession() as db:
        db.add(KbRulesDigest(root_key="local", content="standing rules",
                             char_count=14))
        db.add(KbProcedure(root_key="local", rel="skills/old.md", title="Old",
                           content="old procedure", content_hash="h-old"))
        db.commit()

    (tmp_path / "notes.md").write_text("Unsignalled text the classifier must judge.")
    monkeypatch.setattr(kb_classify, "_llm_classify", lambda blocks: None)  # outage
    monkeypatch.setattr(rag, "embed", lambda texts: [[0.1] * rag.EMBED_DIM for _ in texts])
    reindexed = {"n": 0}
    monkeypatch.setattr(meili, "reindex_kb",
                        lambda docs: reindexed.__setitem__("n", len(docs)) or len(docs))

    n = rag.ingest_knowledge_repo([("local", tmp_path)])
    assert n == 1 and reindexed["n"] == 1  # the retrieval index still swapped

    with SyncSession() as db:
        from sqlalchemy import select
        digest = db.execute(select(KbRulesDigest)).scalars().one()
        proc = db.execute(select(KbProcedure)).scalars().one()
        assert digest.content == "standing rules"  # previous generation kept
        assert proc.title == "Old"


# ------------------------------------------------------------- dispatch narrowing

def test_rules_digests_narrowing_and_kill_switch():
    from app.services import rag

    git_id = str(uuid.uuid4())
    with SyncSession() as db:
        db.add(KbRulesDigest(root_key="local", content="local rules", char_count=11))
        db.add(KbRulesDigest(root_key=git_id, content="git rules", char_count=9))
        db.commit()

    with SyncSession() as db:
        # kb_ids=[] selects nothing
        assert rag.rules_digests(db, []) == []
        # kb_ids=None: local (no row = enabled) is served; the git digest has NO
        # enabled+verified KnowledgeBase row -> kill-switched out immediately
        got = rag.rules_digests(db, None)
        assert [c for _n, c in got] == ["local rules"]

    with SyncSession() as db:
        db.add(KnowledgeBase(kind="git", name="Team KB", id=git_id, uri="x",
                             enabled=True, verified=True))
        db.commit()
    with SyncSession() as db:
        got = rag.rules_digests(db, None)
        assert ("Team KB", "git rules") in got and len(got) == 2
        # a list selection narrows to exactly those sources
        got_git = rag.rules_digests(db, [git_id])
        assert got_git == [("Team KB", "git rules")]


# ------------------------------------------------------------- task injection

def test_build_task_file_injects_digest_above_context_and_fingerprints_it(monkeypatch):
    from app.workers import tasks

    class _Proj:
        id = "p1"
        name = "P"
        description = "d"
        speciality = "general-webapp"
        from_scratch = True
        sovereign = False
        sovereign_comment = None
        kind = "ai"
        kb_ids = None
        dev_request_id = None
        dev_plan = None
        dev_plan_status = None
        org_id = "o1"
        use_global_memory = None

    monkeypatch.setattr(tasks, "_context_repos", lambda db, project: [])
    monkeypatch.setattr(tasks, "_effective_memory", lambda db, project: [])
    monkeypatch.setattr(tasks, "_project_files_meta", lambda db, project: [])
    monkeypatch.setattr(tasks.rag, "search", lambda *a, **k: [])
    digest = "### conv.md\n\nevery commit message must be prefixed by its issue number"
    monkeypatch.setattr(tasks.rag, "rules_digests",
                        lambda db, kb_ids: [("Team KB", digest)])
    from app.services import speciality as spec
    monkeypatch.setattr(spec, "deliverable_clause", lambda p: "x")
    monkeypatch.setattr(spec, "knowledge_tags", lambda p: [])
    monkeypatch.setattr(spec, "one_shot_example", lambda p: "")
    from app.agents import pipeline as pl
    monkeypatch.setattr(pl, "_project_context", lambda db, p: "ctx")
    captured = {}

    def fake_fp(snippets, allow_text):
        captured["snippets"] = list(snippets)
        return ["fp"]
    monkeypatch.setattr(tasks, "_kb_fingerprints", fake_fp)

    text, fps = tasks._build_task_file(None, _Proj())
    assert "## Standing rules from the platform knowledge bases" in text
    assert "Source: Team KB" in text and digest in text
    # trusted region: the digest sits ABOVE the customer-data boundary
    assert text.index("## Standing rules") < text.index("## Project context")
    # the FULL digest text joins the leak-scan fingerprints (never truncated)
    assert digest in captured["snippets"]
    assert fps == ["fp"]


# ------------------------------------------------------------- §KB tiers phase 2: procedures

def test_ingest_stores_procedure_registry_with_block_hash(monkeypatch, tmp_path):
    from app.models import KbProcedure
    from app.services import meili, rag

    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "package.md").write_text(
        "# Offline packaging\nrun make export then make load")
    monkeypatch.setattr(kb_classify, "_llm_classify", _no_llm)  # path signal covers it
    monkeypatch.setattr(rag, "embed", lambda texts: [[0.1] * rag.EMBED_DIM for _ in texts])
    captured = {}

    def _reindex(docs):
        captured["docs"] = docs
        return len(docs)
    monkeypatch.setattr(meili, "reindex_kb", _reindex)

    rag.ingest_knowledge_repo([("local", tmp_path)])
    doc = captured["docs"][0]
    assert doc["content_class"] == "procedure" and doc["block_hash"]
    with SyncSession() as db:
        from sqlalchemy import select
        row = db.execute(select(KbProcedure)).scalars().one()
        assert row.root_key == "local" and row.rel == "skills/package.md"
        assert row.title == "Offline packaging"
        assert row.content_hash == doc["block_hash"]
        assert "make export" in row.content


def test_demoted_rule_joins_procedure_registry(monkeypatch, tmp_path):
    from app.models import KbProcedure
    from app.services import meili, rag

    monkeypatch.setattr(settings, "kb_rules_digest_max_chars", 30)
    (tmp_path / "rules.md").write_text(
        "---\ntype: Rule\n---\nshort rule fits\n\n# Big rule\n" + "x" * 200)
    monkeypatch.setattr(kb_classify, "_llm_classify", _no_llm)
    monkeypatch.setattr(rag, "embed", lambda texts: [[0.1] * rag.EMBED_DIM for _ in texts])
    monkeypatch.setattr(meili, "reindex_kb", lambda docs: len(docs))

    rag.ingest_knowledge_repo([("local", tmp_path)])
    with SyncSession() as db:
        from sqlalchemy import select
        rows = db.execute(select(KbProcedure)).scalars().all()
        assert len(rows) == 1 and rows[0].title == "Big rule"


def test_procedures_for_maps_hits_to_full_bodies(monkeypatch):
    from app.models import KbProcedure
    from app.services import rag

    full = "# Deploy airgapped\nstep 1\nstep 2\n" + "detail " * 100
    h = kb_classify.norm_hash(full)
    with SyncSession() as db:
        db.add(KbProcedure(root_key="local", rel="skills/deploy.md",
                           title="Deploy airgapped", content=full, content_hash=h))
        db.commit()

    monkeypatch.setattr(rag, "_embed_raw", lambda texts: ([[0.0] * rag.EMBED_DIM], {}))
    monkeypatch.setattr(rag.meili, "search_hybrid",
                        lambda vec, q, k, tags=None, content_class=None: [
                            {"content": full[:1500], "source": "kb",
                             "path": "local/skills/deploy.md#0",
                             "file": "local/skills/deploy.md",
                             "content_class": "procedure", "block_hash": h,
                             "score": 0.9}])

    with SyncSession() as db:
        # kb_ids=[] -> nothing, without embedding
        assert rag.procedures_for(db, "deploy the app offline", []) == []
        got = rag.procedures_for(db, "deploy the app offline", None)
    assert len(got) == 1
    src, title, content = got[0]
    assert title == "Deploy airgapped" and content == full  # FULL body, not the chunk
    assert src  # resolved source name


def test_procedures_for_empty_query_and_missing_row(monkeypatch):
    from app.services import rag

    with SyncSession() as db:
        assert rag.procedures_for(db, "   ", None) == []

    monkeypatch.setattr(rag, "_embed_raw", lambda texts: ([[0.0] * rag.EMBED_DIM], {}))
    monkeypatch.setattr(rag.meili, "search_hybrid",
                        lambda vec, q, k, tags=None, content_class=None: [
                            {"content": "c", "source": "kb", "path": "local/x.md#0",
                             "file": "local/x.md", "content_class": "procedure",
                             "block_hash": "nosuchhash", "score": 0.9}])
    with SyncSession() as db:
        # a hit whose block has no registry row (stale index) is skipped
        assert rag.procedures_for(db, "anything", None) == []


def test_search_hybrid_content_class_filter_fails_closed(monkeypatch):
    import httpx
    import json as _json
    from app.services import meili

    captured = {}

    def handler(request):
        body = _json.loads(request.content)
        captured["filter"] = body.get("filter")
        if "content_class" in (body.get("filter") or ""):
            return httpx.Response(400, json={"code": "invalid_search_filter"})
        return httpx.Response(200, json={"hits": [{"content": "x"}]})

    monkeypatch.setattr(meili, "_client", lambda: httpx.Client(
        base_url="http://meili.test", transport=httpx.MockTransport(handler)))

    hits = meili.search_hybrid([0.0] * meili.EMBED_DIM, "q", k=3,
                               content_class="procedure")
    assert captured["filter"] == 'content_class = "procedure"'
    assert hits == []  # fail-closed: never other-tier content


def test_build_task_file_injects_matching_procedures(monkeypatch):
    from app.workers import tasks

    class _Proj:
        id = "p1"
        name = "P"
        description = "d"
        speciality = "general-webapp"
        from_scratch = True
        sovereign = False
        sovereign_comment = None
        kind = "ai"
        kb_ids = None
        dev_request_id = None
        dev_plan = None
        dev_plan_status = None
        org_id = "o1"
        use_global_memory = None

    monkeypatch.setattr(tasks, "_context_repos", lambda db, project: [])
    monkeypatch.setattr(tasks, "_effective_memory", lambda db, project: [])
    monkeypatch.setattr(tasks, "_project_files_meta", lambda db, project: [])
    monkeypatch.setattr(tasks.rag, "search", lambda *a, **k: [])
    monkeypatch.setattr(tasks.rag, "rules_digests", lambda db, kb_ids: [])
    body = "# Offline packaging\nmake export, then verify the archive"
    monkeypatch.setattr(tasks.rag, "procedures_for",
                        lambda db, q, kb_ids, k=None: [("Team KB", "Offline packaging", body)])
    from app.services import speciality as spec
    monkeypatch.setattr(spec, "deliverable_clause", lambda p: "x")
    monkeypatch.setattr(spec, "knowledge_tags", lambda p: [])
    monkeypatch.setattr(spec, "one_shot_example", lambda p: "")
    from app.agents import pipeline as pl
    monkeypatch.setattr(pl, "_project_context", lambda db, p: "ctx")
    captured = {}
    monkeypatch.setattr(tasks, "_kb_fingerprints",
                        lambda snippets, allow: captured.setdefault("s", list(snippets)) or ["fp"])

    text, _ = tasks._build_task_file(None, _Proj())
    assert "## Relevant procedures from the platform knowledge bases" in text
    assert "### Offline packaging (source: Team KB)" in text and body in text
    assert text.index("## Relevant procedures") < text.index("## Project context")
    assert body in captured["s"]  # full body fingerprinted
