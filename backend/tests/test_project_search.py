"""§project search: the ranking behind the dashboard search box.

The property that matters is degradation: the LLM rerank is a relevance bonus,
never a dependency. Disabled, rate-capped, down, or answering nonsense, the
customer must still get the deterministic text ranking - and the model can only
reorder/filter the projects it was handed, never invent one or hide one the text
search plainly matched.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents import pipeline
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import Organization, Project, User
from app.services import project_search


def _p(pid, name, description="", status="development", kind="ai", demo_state="stopped"):
    return SimpleNamespace(id=pid, name=name, description=description, status=status,
                           kind=kind, speciality=None, demo_state=demo_state, created_at=None)


def _snaps(*projects):
    return [project_search.snapshot(p) for p in projects]


RECORDS = _snaps(
    _p("a", "Angry Birds HTML Game", "A physics slingshot game in canvas"),
    _p("b", "Sovereignty chat", "Talking about sovereign hosting", kind="chat"),
    _p("c", "Invoice tracker", "Small internal billing tool", status="awaiting_customer"),
)


def test_snapshot_is_plain_data():
    # The API ranks in a threadpool, so the records must not carry ORM objects.
    rec = project_search.snapshot(_p("a", "Angry Birds", "desc"))
    assert set(rec) == {"id", "name", "description", "speciality", "kind", "status",
                        "demo_state", "created"}
    assert all(isinstance(v, str) for v in rec.values())


def test_name_hit_outranks_description_hit():
    ranked = project_search.deterministic(RECORDS, "game")
    assert [r["id"] for r in ranked] == ["a"] or ranked[0]["id"] == "a"


def test_status_and_kind_are_searchable():
    assert [r["id"] for r in project_search.deterministic(RECORDS, "awaiting customer")] == ["c"]
    assert "b" in [r["id"] for r in project_search.deterministic(RECORDS, "chat")]


def test_unmatched_extra_token_does_not_sink_a_good_match():
    # "angry birds zzz" still finds the Angry Birds project.
    assert project_search.deterministic(RECORDS, "angry birds zzz")[0]["id"] == "a"


def test_empty_query_returns_everything_unranked():
    ids, ai = project_search.search(RECORDS, "   ", use_ai=True)
    assert ids == ["a", "b", "c"] and ai is False


def test_ai_disabled_uses_deterministic_ranking(monkeypatch):
    monkeypatch.setattr(pipeline, "rank_projects",
                        lambda *a, **k: pytest.fail("model must not be called"))
    ids, ai = project_search.search(RECORDS, "game", use_ai=False)
    assert ids == ["a"] and ai is False


def test_model_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(project_search.pipeline, "rank_projects", lambda *a, **k: None)
    ids, ai = project_search.search(RECORDS, "game", use_ai=True)
    assert ids == ["a"] and ai is False


def test_model_ordering_wins_when_it_answers(monkeypatch):
    # The whole point of the rerank: an intent query the text search can't serve.
    monkeypatch.setattr(project_search.pipeline, "rank_projects", lambda *a, **k: ["c", "a"])
    ids, ai = project_search.search(RECORDS, "the thing about money", use_ai=True)
    assert ids == ["c", "a"] and ai is True


def test_model_cannot_hide_an_obvious_text_match(monkeypatch):
    monkeypatch.setattr(project_search.pipeline, "rank_projects", lambda *a, **k: [])
    ids, ai = project_search.search(RECORDS, "game", use_ai=True)
    assert ids == ["a"] and ai is False


def test_model_returning_nothing_is_trusted_when_text_agrees(monkeypatch):
    monkeypatch.setattr(project_search.pipeline, "rank_projects", lambda *a, **k: [])
    ids, ai = project_search.search(RECORDS, "kubernetes operator", use_ai=True)
    assert ids == [] and ai is True


def test_candidates_are_bounded_but_complete_below_the_cap():
    many = _snaps(*[_p(str(i), f"Project {i}") for i in range(project_search.CANDIDATE_CAP + 10)])
    assert len(project_search._candidates(many, "project")) == project_search.CANDIDATE_CAP
    small = RECORDS
    assert len(project_search._candidates(small, "nothing matches this")) == len(small)


def test_rank_projects_drops_invented_and_duplicated_ids(monkeypatch):
    monkeypatch.setattr(pipeline, "chat_json",
                        lambda *a, **k: ({"ids": ["a", "a", "ghost", 7, "b"]}, {}))
    assert pipeline.rank_projects("x", [{"id": "a"}, {"id": "b"}]) == ["a", "b"]


def test_rank_projects_returns_none_on_model_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(pipeline, "chat_json", boom)
    assert pipeline.rank_projects("x", [{"id": "a"}]) is None


def test_rank_projects_returns_none_on_unusable_answer(monkeypatch):
    monkeypatch.setattr(pipeline, "chat_json", lambda *a, **k: ({"nope": True}, {}))
    assert pipeline.rank_projects("x", [{"id": "a"}]) is None


# --- route: GET /api/projects/search ---

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def customer(client):
    email, pwd = f"ps-{uuid.uuid4().hex[:8]}@example.com", "ps-secret-123"
    with SyncSession() as db:
        org = Organization(name="PS Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        for name, desc in [("Angry Birds Game", "canvas physics game"),
                           ("Invoice tracker", "billing for freelancers")]:
            db.add(Project(org_id=org.id, name=name, description=desc, kind="ai",
                           speciality="general-webapp", status="development",
                           from_scratch=True, sovereign=False))
        db.commit()
        org_id = org.id
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    yield org_id
    with SyncSession() as db:
        db.query(Project).filter_by(org_id=org_id).delete()
        db.query(User).filter_by(org_id=org_id).delete()
        db.query(Organization).filter_by(id=org_id).delete()
        db.commit()


def test_search_route_is_not_swallowed_by_the_project_id_route(client, customer, monkeypatch):
    """`/search` MUST stay declared before `/{project_id}` - otherwise FastAPI
    reads "search" as an id and the box 404s."""
    monkeypatch.setattr(project_search.pipeline, "rank_projects", lambda *a, **k: None)
    r = client.get("/api/projects/search?q=game")
    assert r.status_code == 200, r.text
    assert [p["name"] for p in r.json()["results"]] == ["Angry Birds Game"]


def test_search_empty_query_returns_the_whole_list(client, customer):
    r = client.get("/api/projects/search")
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 2 and body["ai"] is False and body["reason"] is None


def test_search_reports_why_the_ranking_is_not_ai(client, customer, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "project_search_ai_enabled", False)
    body = client.get("/api/projects/search?q=game").json()
    assert body["ai"] is False and body["reason"] == "disabled"

    monkeypatch.setattr(settings, "project_search_ai_enabled", True)
    monkeypatch.setattr(project_search.pipeline, "rank_projects", lambda *a, **k: None)
    body = client.get("/api/projects/search?q=game").json()
    assert body["ai"] is False and body["reason"] == "unavailable"


def test_search_never_leaks_another_orgs_projects(client, customer, monkeypatch):
    monkeypatch.setattr(project_search.pipeline, "rank_projects", lambda *a, **k: None)
    with SyncSession() as db:
        other = Organization(name="Other Org")
        db.add(other)
        db.flush()
        db.add(Project(org_id=other.id, name="Angry Birds Secret", description="not yours",
                       kind="ai", speciality="general-webapp", status="development",
                       from_scratch=True, sovereign=False))
        db.commit()
        other_id = other.id
    try:
        names = [p["name"] for p in client.get("/api/projects/search?q=angry").json()["results"]]
        assert names == ["Angry Birds Game"]
    finally:
        with SyncSession() as db:
            db.query(Project).filter_by(org_id=other_id).delete()
            db.query(Organization).filter_by(id=other_id).delete()
            db.commit()
