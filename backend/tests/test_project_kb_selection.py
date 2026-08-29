"""Per-project KB selection (§KB): Project.kb_ids narrows which KnowledgeBase rows
feed a project's dev runs. rag-side: selected_root_keys maps a selection to the
Meili doc namespaces it may read (local row → the literal 'local', git rows →
their ids, gated on enabled+verified), retrieval root-filters hits fail-closed and
short-circuits WITHOUT embedding when the selection matches no active source.
API-side: the admin PATCH validates ids loudly, [] selects none, an explicit null
CLEARS the selection to [] (no state means "every KB on the instance"), and an
omitted field leaves the selection untouched.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import KnowledgeBase, Organization, Project, User
from app.seed import seed_knowledge_bases
from app.services import rag


@pytest.fixture(autouse=True)
def _seeded():
    seed_knowledge_bases()


@pytest.fixture
def db():
    with SyncSession() as s:
        try:
            yield s
        finally:
            s.rollback()


def _local_id(db):
    return db.execute(select(KnowledgeBase.id).where(
        KnowledgeBase.kind == "local")).scalar_one()


def _git(db, **kw):
    kw.setdefault("name", "Git KB")
    kw.setdefault("uri", "ssh://git@example.com/kb.git")
    kw.setdefault("enabled", True)
    kw.setdefault("verified", True)
    kb = KnowledgeBase(kind="git", **kw)
    db.add(kb)
    db.flush()
    return kb


# ------------------------------------------------------------------ rag-side

def test_selected_root_keys_mapping(db):
    local_id = _local_id(db)
    ok = _git(db)
    off = _git(db, name="Off", enabled=False)
    unverified = _git(db, name="Unverified", verified=False)
    roots = rag.selected_root_keys(
        db, [local_id, ok.id, off.id, unverified.id, "no-such-id"])
    # Effective = enabled AND verified AND selected; the local row maps to the
    # literal 'local' namespace its docs are ingested under.
    assert roots == {"local", ok.id}
    assert rag.selected_root_keys(db, None) is None
    assert rag.selected_root_keys(db, []) == set()


def test_empty_selection_short_circuits_without_embedding(db, monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "_embed_raw",
                        lambda texts: calls.append(texts) or ([[0.0]], {}))
    assert rag.search(db, "anything", kb_ids=[]) == []
    assert rag.retrieve(db, "anything", kb_ids=[]) == ([], [])
    # A selection referencing only inactive sources is as cheap as [].
    off = _git(db, name="Off", enabled=False)
    assert rag.search(db, "anything", kb_ids=[off.id]) == []
    assert calls == []


def test_retrieval_root_filters_hits(db, monkeypatch):
    local_id = _local_id(db)
    git = _git(db)
    docs = [
        {"content": "local doc", "path": "local/notes.md#0",
         "file": "local/notes.md", "score": 0.9},
        {"content": "git doc", "path": f"{git.id}/README.md#0",
         "file": f"{git.id}/README.md", "score": 0.9},
    ]
    monkeypatch.setattr(rag, "_embed_raw", lambda texts: (
        [[0.0]], {"model": "m", "input_tokens": 1, "output_tokens": 0}))
    monkeypatch.setattr(rag.meili, "search_hybrid",
                        lambda vec, q, k, tags=None: docs)
    assert [h.content for h in rag.search(db, "q", k=4, kb_ids=[local_id])] == ["local doc"]
    assert [h.content for h in rag.search(db, "q", k=4, kb_ids=[git.id])] == ["git doc"]
    assert len(rag.search(db, "q", k=4, kb_ids=[local_id, git.id])) == 2
    assert len(rag.search(db, "q", k=4)) == 2  # no selection → unrestricted
    hits, usages = rag.retrieve(db, "q", k=4, kb_ids=[local_id])
    assert [h.content for h in hits] == ["local doc"]
    assert len(usages) == 1  # the query embedding is still metered


# ------------------------------------------------------------------ HTTP surface

@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    # Heal the async pools an earlier HTTP module left bound to its now-closed
    # loop (see test_org_memory) so a login here doesn't hit a foreign loop.
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _admin_with_project(client):
    email = f"kbsel-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "kbsel-secret-123"
    with SyncSession() as db:
        org = Organization(name="KBSel Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role="admin", email_verified=True))
        project = Project(org_id=org.id, name="KBSel", description="d")
        db.add(project)
        db.commit()
        pid = project.id
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}, pid


def test_admin_patch_kb_ids(client):
    h, pid = _admin_with_project(client)
    with SyncSession() as db:
        kb_id = _local_id(db)

    r = client.patch(f"/api/admin/projects/{pid}", json={"kb_ids": [kb_id, kb_id]},
                     headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["kb_ids"] == [kb_id]  # deduped

    # Unknown ids are rejected loudly (a UI bug must not silently drop KBs).
    r = client.patch(f"/api/admin/projects/{pid}",
                     json={"kb_ids": ["no-such-kb"]}, headers=h)
    assert r.status_code == 422

    # Omitting the field leaves the selection untouched...
    r = client.patch(f"/api/admin/projects/{pid}", json={"tier": "mvp"}, headers=h)
    assert r.status_code == 200 and r.json()["kb_ids"] == [kb_id]

    # ...an explicit [] selects none, and so does null: there is no state that
    # means "every KB on the instance", so the route can never write one back.
    r = client.patch(f"/api/admin/projects/{pid}", json={"kb_ids": []}, headers=h)
    assert r.status_code == 200 and r.json()["kb_ids"] == []
    r = client.patch(f"/api/admin/projects/{pid}", json={"kb_ids": [kb_id]}, headers=h)
    assert r.status_code == 200 and r.json()["kb_ids"] == [kb_id]
    r = client.patch(f"/api/admin/projects/{pid}", json={"kb_ids": None}, headers=h)
    assert r.status_code == 200 and r.json()["kb_ids"] == []


def test_admin_patch_dev_pod_resources(client):
    """§dev-pod resources: per-project dev-run pod scheduling requests round-trip
    through the admin PATCH (docker-style values, null resets, junk 422s), and
    ride the dev dispatch to the deployer."""
    h, pid = _admin_with_project(client)

    r = client.patch(f"/api/admin/projects/{pid}",
                     json={"dev_cpu_request": "0.5", "dev_mem_request": "4g"},
                     headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["dev_cpu_request"] == "0.5"
    assert r.json()["dev_mem_request"] == "4g"

    # a non-quantity is rejected loudly; the stored values stay
    r = client.patch(f"/api/admin/projects/{pid}",
                     json={"dev_cpu_request": "lots"}, headers=h)
    assert r.status_code == 422
    r = client.patch(f"/api/admin/projects/{pid}", json={"tier": "mvp"}, headers=h)
    assert r.json()["dev_cpu_request"] == "0.5"

    # null resets to the deployer's instance defaults
    r = client.patch(f"/api/admin/projects/{pid}",
                     json={"dev_cpu_request": None, "dev_mem_request": None},
                     headers=h)
    assert r.status_code == 200
    assert r.json()["dev_cpu_request"] is None
    assert r.json()["dev_mem_request"] is None


def test_legacy_null_kb_ids_reads_nothing():
    """§KB opt-in: a legacy NULL row resolves to NO knowledge bases.

    KnowledgeBase is an instance-wide list with no org_id, so the old "null =
    every enabled KB" default put one tenant's corpus in another tenant's chat
    and build context. `rag.project_kb_ids` is the single boundary that every
    project-scoped read goes through, so pinning it here pins all of them.
    """
    from types import SimpleNamespace

    assert rag.project_kb_ids(SimpleNamespace(kb_ids=None)) == []
    assert rag.project_kb_ids(SimpleNamespace(kb_ids=[])) == []
    assert rag.project_kb_ids(SimpleNamespace(kb_ids=["a", "b"])) == ["a", "b"]


def test_every_project_scoped_kb_read_goes_through_the_resolver():
    """Source guard: no project-scoped retrieval may pass `project.kb_ids` raw.

    The fix is one helper, and a new call site that reaches around it silently
    restores the cross-tenant default for that path only - which is exactly how
    this got missed the first time.
    """
    import pathlib as _p
    import re

    root = _p.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for f in list(root.rglob("*.py")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"(?<!rag\.)project_kb_ids", line):
                continue
            if re.search(r"\bproject\.kb_ids\b", line) and "rag.project_kb_ids" not in line:
                # the resolver itself, plus the two write paths (the admin
                # PATCH and the §project defaults creation stamp) and the
                # serializer, which touch the stored column on purpose
                if f.name in ("rag.py", "admin.py", "project_defaults.py",
                              "serializers.py"):
                    continue
                offenders.append(f"{f.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, "pass rag.project_kb_ids(project), not project.kb_ids:\n" + "\n".join(offenders)
