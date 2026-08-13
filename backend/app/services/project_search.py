"""Project search (§project search): the ranking behind the dashboard search box.

Two layers, in this order:

1. a deterministic matcher over the text the customer can actually see on a
   project card (name, speciality label, kind, status, demo state) plus the
   description - instant, free, and the answer whenever the model is off;
2. an LLM rerank (`pipeline.rank_projects`, prompt #13) that reorders/filters a
   BOUNDED candidate set so intent-level queries work ("the game whose build
   failed"), which plain substring matching can never serve.

The LLM is a relevance improvement, never a dependency: it is unbilled, capped
per org by the API, and every failure path - disabled, rate-capped, provider
down, unusable answer - falls back to layer 1. The candidate cap is what keeps a
single search bounded in cost no matter how many projects an org accumulates;
below it EVERY project is offered to the model, so a semantic query is never
narrowed away by the substring pass first.

Ranking runs on plain snapshots, not ORM rows: the model call blocks, so the API
hands this to a threadpool, and a detached ORM object touched off the event loop
is how lazy-load explosions happen. `snapshot()` is called in the request thread;
everything below it is pure data.
"""
from __future__ import annotations

import logging
import re

from app.agents import pipeline
from app.services import speciality

log = logging.getLogger(__name__)

# Most projects handed to the model in one search. Orgs are far below this today;
# it exists so an outlier org can't turn one keystroke into a huge prompt.
CANDIDATE_CAP = 60
# Per-project description text the model sees. Enough to convey the subject,
# short enough that a full candidate set stays a small prompt.
DESCRIPTION_CHARS = 400
QUERY_CHARS = 200

_KIND_LABELS = {"ai": "curated ai build", "direct_quote": "direct quote engagement",
                "auto_dev": "auto-developer sentinel", "chat": "chat conversation"}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _speciality_label(project) -> str:
    prof = speciality.profile(getattr(project, "speciality", None))
    return prof.get("short_label") or prof.get("label") or (project.speciality or "")


def snapshot(project) -> dict:
    """Flatten one ORM project into the plain record the ranking works on. Must
    be called on the request thread (it reads mapped attributes)."""
    return {
        "id": project.id,
        "name": project.name or "",
        "description": (project.description or "")[:DESCRIPTION_CHARS],
        "speciality": _speciality_label(project),
        "kind": _KIND_LABELS.get(project.kind, project.kind or ""),
        "status": (project.status or "").replace("_", " "),
        "demo_state": (project.demo_state or "").replace("_", " "),
        "created": project.created_at.date().isoformat() if project.created_at else "",
    }


def _haystack(rec: dict) -> str:
    """Everything a customer could reasonably be searching by, lowercased: the
    card's visible text plus the description behind it."""
    return " ".join([rec["name"], rec["speciality"], rec["kind"], rec["status"],
                     rec["demo_state"], rec["description"]]).lower()


def score(rec: dict, query: str) -> float:
    """Deterministic relevance in the customer's terms: a name hit outweighs a
    body hit, a whole-phrase hit outweighs scattered tokens, and a token the
    project doesn't carry costs nothing (an unmatched extra word must not push a
    good match below a weak one)."""
    q = query.lower().strip()
    if not q:
        return 0.0
    name = rec["name"].lower()
    hay = _haystack(rec)
    total = 0.0
    if q in name:
        total += 6.0
    elif q in hay:
        total += 3.0
    for token in set(_TOKEN_RE.findall(q)):
        if token in name:
            total += 2.0
        elif token in hay:
            total += 1.0
    return total


def deterministic(records: list[dict], query: str) -> list[dict]:
    """Records that match textually, best first. Ties keep the caller's order,
    which is newest-first - so equally-good matches read as the list does."""
    hits = [(score(r, query), i, r) for i, r in enumerate(records)]
    hits = [h for h in hits if h[0] > 0]
    hits.sort(key=lambda t: (-t[0], t[1]))
    return [r for _s, _i, r in hits]


def _candidates(records: list[dict], query: str) -> list[dict]:
    """The records offered to the model, bounded by CANDIDATE_CAP. Under the cap
    the whole list goes (a semantic query must not be pre-filtered away); over it,
    textual matches come first and the newest fill the rest."""
    if len(records) <= CANDIDATE_CAP:
        return list(records)
    picked = deterministic(records, query)[:CANDIDATE_CAP]
    seen = {r["id"] for r in picked}
    for r in records:  # newest-first from the caller
        if len(picked) >= CANDIDATE_CAP:
            break
        if r["id"] not in seen:
            picked.append(r)
    return picked


def search(records: list[dict], query: str, use_ai: bool) -> tuple[list[str], bool]:
    """Rank `records` (newest-first snapshots) for `query`. Returns the ordered
    project ids plus whether the model's ranking is the one being returned.

    Blocking (it makes the model call) - the API runs it in a threadpool."""
    query = (query or "").strip()[:QUERY_CHARS]
    if not query:
        return [r["id"] for r in records], False
    fallback = [r["id"] for r in deterministic(records, query)]
    if not use_ai or not records:
        return fallback, False
    candidates = _candidates(records, query)
    ids = pipeline.rank_projects(query, candidates)
    if ids is None:
        return fallback, False
    # An empty model answer is only trusted when the text search agrees there is
    # nothing: a model that drops everything must not hide a project the customer
    # can plainly see matches.
    if not ids and fallback:
        return fallback, False
    return ids, True
