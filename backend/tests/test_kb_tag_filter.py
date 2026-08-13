"""§Phase 2: speciality KB-tag retrieval filter (meili._tag_filter + rag wiring).

A KB organised by top-level folder (/knowledge/aws-well-architected/…, /kubernetes-hardening/…)
is tag-indexed on ingest, and a dev run retrieves only its speciality's tags PLUS
untagged/general docs - so a sovereign build keeps OCPA/CVE chunks but drops the
aws-only distractors. The filter fails OPEN (no tags -> no filter; a Meili filter
error -> unfiltered), never breaking retrieval.
"""
from app.services import meili, rag


def test_tag_filter_none_when_no_tags():
    assert meili._tag_filter(None) is None
    assert meili._tag_filter([]) is None
    assert meili._tag_filter(["", "  "]) is None


def test_tag_filter_includes_tags_and_untagged():
    f = meili._tag_filter(["ocpa", "kubernetes-hardening"])
    assert 'tags IN ["ocpa", "kubernetes-hardening"]' in f
    assert "tags IS EMPTY" in f and "tags IS NULL" in f  # untagged/general docs kept


def test_tag_filter_strips_quotes_defensively():
    # a stray quote in a tag value is stripped so it can't break the filter string
    f = meili._tag_filter(['a"b', "c"])
    assert '"ab"' in f and '"c"' in f


def test_rag_search_forwards_tags_to_meili(monkeypatch):
    captured = {}

    def fake_search(vec, q, k, tags=None):
        captured["tags"] = tags
        return []
    monkeypatch.setattr(rag, "kb_retrieval_enabled", lambda db: True)
    monkeypatch.setattr(rag, "_embed_raw", lambda qs: ([[0.0] * 4], {"model": "m", "input_tokens": 1, "output_tokens": 0}))
    monkeypatch.setattr(rag.meili, "search_hybrid", fake_search)
    rag.search(None, "q", k=6, tags=["kubernetes-hardening", "ocpa"])
    assert captured["tags"] == ["kubernetes-hardening", "ocpa"]
