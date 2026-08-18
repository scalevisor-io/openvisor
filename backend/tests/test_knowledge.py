"""Knowledge/MCP metering + leak-protection tests. The DB-backed cases use a
SyncSession and roll back, so they don't pollute the dev database."""
import pytest
from sqlalchemy import select

from app.core.db import SyncSession
from app.models import CreditTransaction, KnowledgeChunk, Organization, Project
from app.services import knowledge
from app.services.pricing import cost_credits


def _chunk(content, path="kb/doc.md#0", file="kb/doc.md", source="kb"):
    return KnowledgeChunk(source=source, path=path, content=content, meta={"file": file})


# ---- pure: verbatim leak guard ----
def test_verbatim_guard_redacts_long_runs_but_keeps_paraphrase():
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliett kilo lima mike november oscar"
    chunk_text = f"The reference lists {words} as ordered tokens."
    leak = f"Per the notes: {words} - and then some commentary."
    redacted, hit = knowledge._verbatim_guard(leak, [chunk_text])
    assert hit is True
    assert "[…]" in redacted
    # the long verbatim run must be gone
    assert "alpha bravo charlie delta echo foxtrot golf hotel india juliett kilo lima" not in redacted

    paraphrase = "The passage simply enumerates fifteen NATO phonetic code words in their usual order."
    out, hit2 = knowledge._verbatim_guard(paraphrase, [chunk_text])
    assert hit2 is False
    assert out == paraphrase


# ---- pure: citations never carry chunk bodies ----
def test_build_citations_excludes_content():
    chunks = [_chunk("SECRET internal knowledge body one"),
              _chunk("SECRET internal knowledge body two", path="kb/two.md#3", file="kb/two.md")]
    cites = knowledge._build_citations(chunks)
    assert cites == [
        {"n": 1, "source": "kb", "ref": "kb/doc.md"},
        {"n": 2, "source": "kb", "ref": "kb/two.md"},
    ]
    for c in cites:
        assert "content" not in c
        assert "SECRET" not in str(c)


# ---- DB-backed: metering debits the wallet through the project, rolled back after ----
def test_answer_question_meters_through_the_project(monkeypatch):
    emb_usage = {"model": "mistral-embed", "input_tokens": 120, "output_tokens": 0}
    syn_usage = {"model": "mistral-large-latest", "input_tokens": 200, "output_tokens": 50}
    chunks = [_chunk("Some knowledge about sovereign cloud isolation patterns.")]

    monkeypatch.setattr("app.services.rag.retrieve",
                        lambda db, q, k=6, **kw: (chunks, [emb_usage]))
    monkeypatch.setattr(knowledge, "_synthesize",
                        lambda q, ch, cfg: ("A paraphrased answer with a citation [1].",
                                            [syn_usage]))
    # The pre-spend priced-models gate reads the embedding model from settings and
    # the synthesis model from the PROJECT's resolved config: pin both so the test
    # stays hermetic whatever the instance .env says.
    monkeypatch.setattr(knowledge.settings, "embedding_model", emb_usage["model"])
    monkeypatch.setattr(knowledge.model_config, "project_model_config",
                        lambda db, project: ("https://api.example.test/v1", "k",
                                             syn_usage["model"]))

    expected = cost_credits(**{k: emb_usage[k] for k in ("input_tokens", "output_tokens")},
                            api_model=emb_usage["model"]) + \
        cost_credits(api_model=syn_usage["model"], input_tokens=syn_usage["input_tokens"],
                     output_tokens=syn_usage["output_tokens"])

    with SyncSession() as db:
        org = Organization(name="Knowledge Test Org", credit_balance=100.0)
        db.add(org)
        db.flush()
        oid = org.id
        project = Project(org_id=oid, name="MCP consult", kind="mcp", description="")
        db.add(project)
        db.flush()

        result = knowledge.answer_question(db, oid, "how does isolation work?", k=6,
                                           project=project)

        assert result["credits_charged"] == pytest.approx(expected)
        assert result["citations"] == [{"n": 1, "source": "kb", "ref": "kb/doc.md"}]
        assert org.credit_balance == pytest.approx(100.0 - expected)
        txns = db.execute(
            select(CreditTransaction).where(CreditTransaction.org_id == oid)).scalars().all()
        assert len(txns) == 1
        # Attributed to the asking project, not an anonymous org-level row.
        assert txns[0].kind == "consumption"
        assert txns[0].project_id == project.id
        assert txns[0].amount == pytest.approx(-expected)

        db.rollback()


def test_answer_question_needs_a_project(monkeypatch):
    """An account-wide token has no project - so no model, no KB selection and
    nothing to bill. The query is refused before any RAG or spend."""
    monkeypatch.setattr("app.services.rag.retrieve",
                        lambda *a, **k: pytest.fail("must not run RAG without a project"))
    with SyncSession() as db:
        org = Organization(name="No Project Org", credit_balance=100.0)
        db.add(org)
        db.flush()
        with pytest.raises(knowledge.KnowledgeConfigError):
            knowledge.answer_question(db, org.id, "anything", k=6)
        db.rollback()


def test_answer_question_rejects_empty_wallet(monkeypatch):
    monkeypatch.setattr("app.services.rag.retrieve",
                        lambda *a, **k: pytest.fail("must not run RAG when broke"))
    with SyncSession() as db:
        org = Organization(name="Broke Org", credit_balance=0.0)
        db.add(org)
        db.flush()
        project = Project(org_id=org.id, name="MCP consult", kind="mcp", description="")
        db.add(project)
        db.flush()
        with pytest.raises(knowledge.InsufficientCredits):
            knowledge.answer_question(db, org.id, "anything", k=6, project=project)
        db.rollback()
