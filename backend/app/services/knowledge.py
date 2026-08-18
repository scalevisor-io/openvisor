"""Billable knowledge answering for the MCP `search_knowledge` tool.

Retrieves KB passages (Meilisearch hybrid), synthesizes a cited answer WITHOUT
leaking raw chunks, and meters the query-embedding + synthesis model cost (×
CREDIT_MARKUP) against the org wallet. Sync (SyncSession) - the async endpoint
offloads it via run_in_threadpool. Caller commits (matches llm.record_usage)."""
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import KnowledgeChunk, Organization, Project
from app.services import brand, llm, model_config, rag
from app.services.pricing import is_priced

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "agents" / "prompts"
MIN_VERBATIM_RUN = 12  # words; a run this long echoing a chunk is treated as a leak


class InsufficientCredits(Exception):
    """The org wallet can't cover a knowledge query."""


class KnowledgeConfigError(Exception):
    """A model in the knowledge path isn't priced (would bill 0) / org missing."""


def _load_prompt(name: str) -> str:
    return brand.render((PROMPT_DIR / name).read_text())


def _norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace - for verbatim matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def _verbatim_guard(answer: str, chunk_texts: list[str],
                    min_run: int = MIN_VERBATIM_RUN) -> tuple[str, bool]:
    """Redact any run of >= min_run consecutive answer words that appears verbatim
    (normalized) in a source chunk, replacing it with '[…]'. Returns (answer, hit).
    Structural defence against echoing raw KB text; mirrors runner/leak_scan.py."""
    words = answer.split()
    if len(words) < min_run:
        return answer, False
    norm_chunks = [_norm(t) for t in chunk_texts]
    redacted: list[str | None] = list(words)
    n = len(words)
    i = 0
    hit = False
    while i <= n - min_run:
        window = _norm(" ".join(words[i:i + min_run]))
        if window and any(window in nc for nc in norm_chunks):
            j = i + min_run
            while j < n and any(_norm(" ".join(words[i:j + 1])) in nc for nc in norm_chunks):
                j += 1
            for x in range(i, j):
                redacted[x] = None
            hit = True
            i = j
        else:
            i += 1
    if not hit:
        return answer, False
    out: list[str] = []
    prev_red = False
    for w in redacted:
        if w is None:
            if not prev_red:
                out.append("[…]")
            prev_red = True
        else:
            out.append(w)
            prev_red = False
    return " ".join(out), True


def _build_citations(chunks: list[KnowledgeChunk]) -> list[dict]:
    """Citation references only - never the chunk body (that's the confidential
    source material we're protecting)."""
    cites = []
    for i, c in enumerate(chunks, 1):
        ref = (c.meta or {}).get("file") or c.path
        cites.append({"n": i, "source": c.source, "ref": ref})
    return cites


def _synthesize(query: str, chunks: list[KnowledgeChunk],
                model_cfg: tuple[str, str, str]) -> tuple[str, list[dict]]:
    """One (or, on a verbatim leak, two) synthesis LLM call(s). Returns the answer
    and the list of usages incurred (so all attempts are billed). `model_cfg` is the
    owning project's (base_url, api_key, model) - the same resolution its chat and
    dev runs use, so one project answers on one model."""
    base_url, api_key, model = model_cfg
    passages = "\n\n".join(f"[{i + 1}] {c.content}" for i, c in enumerate(chunks))
    messages = [
        {"role": "system", "content": _load_prompt("knowledge_synthesis.md")},
        {"role": "user", "content": f"Question: {query}\n\nPassages:\n{passages}"},
    ]
    answer, usage = llm.chat(messages, max_tokens=800, base_url=base_url,
                             api_key=api_key, model=model)
    usages = [usage]
    chunk_texts = [c.content for c in chunks]
    if _verbatim_guard(answer, chunk_texts)[1]:
        # the model copied source text - ask once for a full rewrite
        messages += [
            {"role": "assistant", "content": answer},
            {"role": "user", "content":
                "You reproduced source text verbatim. Rewrite the answer entirely in your "
                "own words, keep the [n] citations, and never copy more than a few words."},
        ]
        answer, usage2 = llm.chat(messages, max_tokens=800, base_url=base_url,
                                  api_key=api_key, model=model)
        usages.append(usage2)
    return answer, usages


def answer_question(db: Session, org_id: str, query: str, k: int = 6,
                    project: Project | None = None) -> dict:
    """Answer a knowledge query and bill it. Raises InsufficientCredits /
    KnowledgeConfigError / llm.LLMUnavailable. Caller commits.

    §MCP privacy: the caller's QUESTION is never persisted - not in the ledger
    detail, not anywhere. An MCP query comes from someone's terminal agent and
    that conversation is theirs; we keep the counters and the token cost, not
    the text.

    A `project` is REQUIRED: the project is what decides which model answers
    (services.model_config) and which knowledge bases it may read (its kb_ids
    opt-in, exactly as project chat scopes retrieval), and it is what the query
    bills to. Only project-scoped MCP tokens reach this path - an account-wide
    token has no project and therefore no such answer to give.
    """
    if project is None:
        raise KnowledgeConfigError("knowledge queries require a project-scoped token")
    org = db.get(Organization, org_id)
    if org is None:
        raise KnowledgeConfigError("Unknown organization")
    if (org.credit_balance or 0.0) <= 0:
        raise InsufficientCredits("wallet empty")

    # The project's own model, resolved exactly as its chat and dev runs resolve it.
    model_cfg = model_config.project_model_config(db, project)

    # Fail loud BEFORE spending: refuse if any model in the path isn't priced. The KB
    # path is query-embedding + synthesis only - Meilisearch hybrid ranks the passages
    # natively, so no reranker model is billed here. The synthesis model checked here
    # is the project's resolved one, not the instance default it may override.
    models = [settings.embedding_model, model_cfg[2]]
    unpriced = [m for m in models if not is_priced(m)]
    if unpriced:
        raise KnowledgeConfigError(f"unpriced knowledge models: {', '.join(unpriced)}")

    # §KB opt-in: narrow retrieval to what this project was actually given, the
    # same way the chat answer path does. null = every enabled source (legacy
    # rows), [] = none, a list = that selection intersected with what is enabled.
    chunks, usages = rag.retrieve(db, query, k, kb_ids=project.kb_ids)
    if not chunks:
        credits = _bill(db, project, usages, "MCP knowledge query (no match)")
        return {"answer": "I don't have anything on that in the knowledge base.",
                "citations": [], "credits_charged": credits}

    answer, syn_usages = _synthesize(query, chunks, model_cfg)
    usages = usages + syn_usages
    answer, redacted = _verbatim_guard(answer, [c.content for c in chunks])
    if redacted:
        log.warning("knowledge answer redacted verbatim KB spans (org=%s)", org_id)
    credits = _bill(db, project, usages, "MCP knowledge query")
    return {"answer": answer, "citations": _build_citations(chunks), "credits_charged": credits}


def _bill(db: Session, project: Project, usages: list[dict], detail: str) -> float:
    """One ledger row per query, attributed to the asking project (its counters
    plus a project consumption row). The detail NEVER carries the question - see
    answer_question."""
    return llm.record_project_usage(db, project, usages, detail)
