"""Project files (Memory & files tab): the /api/projects/{id}/files CRUD surface
(upload/replace/list/download/delete, filename + size + count guards, org scoping)
and the dev-pipeline side - _prepare_runner_inputs staging every file into
.openvisor/files/ (fresh per dispatch) and _build_task_file listing them.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import files as files_api
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import Organization, Project, ProjectFile, User
from app.workers import tasks


@pytest.fixture
def org():
    with SyncSession() as db:
        o = Organization(name="Files Org", credit_balance=10.0)
        db.add(o)
        db.commit()
        oid = o.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(ProjectFile).where(ProjectFile.project_id.in_(pids)))
            db.execute(delete(User).where(User.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    # Same async-pool healing as the other HTTP test modules: drop pools an earlier
    # module left bound to its now-closed event loop.
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    # This module's logins must not eat into the shared login rate-limit window
    # (rl:login:testclient, 20/900s across the whole suite): clear it around the
    # module so the suite's net login budget is unchanged.
    events.get_sync_redis().delete("rl:login:testclient")
    with TestClient(app) as c:
        yield c
    events.get_sync_redis().delete("rl:login:testclient")


def _customer(org_id):
    email = f"pfiles-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "customer-secret-123"
    with SyncSession() as db:
        db.add(User(org_id=org_id, email=email, password_hash=hash_password(pwd),
                    role="customer", email_verified=True))
        p = Project(org_id=org_id, name="P", description="d", kind="ai", status="draft")
        db.add(p)
        db.commit()
        return email, pwd, p.id


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


def _upload(client, pid, h, *files):
    return client.post(f"/api/projects/{pid}/files", headers=h,
                       files=[("files", f) for f in files])


# ---------------------------------------------------------------- HTTP surface

def test_files_require_auth(client):
    assert client.get("/api/projects/any/files").status_code in (401, 403)


def test_upload_list_download_replace_delete(org, client):
    email, pwd, pid = _customer(org)
    h = _auth(client, email, pwd)

    r = _upload(client, pid, h,
                ("spec.md", b"# The spec", "text/markdown"),
                ("data.csv", b"a,b\n1,2\n", "text/csv"))
    assert r.status_code == 201, r.text
    by_name = {f["filename"]: f for f in r.json()}
    assert by_name["spec.md"]["size_bytes"] == len(b"# The spec")
    assert by_name["spec.md"]["author"] == "customer"

    rows = client.get(f"/api/projects/{pid}/files", headers=h).json()
    assert [f["filename"] for f in rows] == ["data.csv", "spec.md"]

    fid = by_name["spec.md"]["id"]
    dl = client.get(f"/api/projects/{pid}/files/{fid}", headers=h)
    assert dl.status_code == 200 and dl.content == b"# The spec"
    assert 'filename="spec.md"' in dl.headers["content-disposition"]

    # Re-uploading a filename replaces in place (same row id, new content).
    r = _upload(client, pid, h, ("spec.md", b"# The spec v2", "text/markdown"))
    assert r.status_code == 201 and r.json()[0]["id"] == fid
    assert client.get(f"/api/projects/{pid}/files/{fid}", headers=h).content == b"# The spec v2"
    assert len(client.get(f"/api/projects/{pid}/files", headers=h).json()) == 2

    assert client.delete(f"/api/projects/{pid}/files/{fid}", headers=h).json() == {"ok": True}
    assert client.get(f"/api/projects/{pid}/files/{fid}", headers=h).status_code == 404
    assert len(client.get(f"/api/projects/{pid}/files", headers=h).json()) == 1


def test_upload_guards(org, client, monkeypatch):
    email, pwd, pid = _customer(org)
    h = _auth(client, email, pwd)

    # A filename with a path component is rejected, not silently stripped.
    for bad in ("../evil.txt", "a/b.txt", "..", ".", ""):
        r = _upload(client, pid, h, (bad, b"x", "text/plain"))
        assert r.status_code in (400, 422), bad

    monkeypatch.setattr(files_api, "MAX_FILE_BYTES", 10)
    r = _upload(client, pid, h, ("big.bin", b"x" * 11, "application/octet-stream"))
    assert r.status_code == 413

    monkeypatch.setattr(files_api, "MAX_FILES_PER_PROJECT", 1)
    r = _upload(client, pid, h, ("one.txt", b"1", "text/plain"),
                ("two.txt", b"2", "text/plain"))
    assert r.status_code == 409
    # Replacing an existing file stays allowed at the cap.
    assert _upload(client, pid, h, ("one.txt", b"1", "text/plain")).status_code == 201
    assert _upload(client, pid, h, ("one.txt", b"1b", "text/plain")).status_code == 201


def test_files_scoped_to_own_org(org, client):
    email, pwd, pid = _customer(org)
    _auth(client, email, pwd)
    _upload(client, pid, _auth(client, email, pwd), ("s.txt", b"x", "text/plain"))

    with SyncSession() as db:
        other = Organization(name="Other Org")
        db.add(other)
        db.commit()
        other_id = other.id
    try:
        email2, pwd2, _pid2 = _customer(other_id)
        h2 = _auth(client, email2, pwd2)
        assert client.get(f"/api/projects/{pid}/files", headers=h2).status_code == 404
    finally:
        with SyncSession() as db:
            opids = db.execute(select(Project.id).where(Project.org_id == other_id)).scalars().all()
            if opids:
                db.execute(delete(ProjectFile).where(ProjectFile.project_id.in_(opids)))
            db.execute(delete(User).where(User.org_id == other_id))
            db.execute(delete(Project).where(Project.org_id == other_id))
            db.execute(delete(Organization).where(Organization.id == other_id))
            db.commit()


# ---------------------------------------------------------------- dev pipeline

def test_prepare_runner_inputs_stages_files(org, tmp_path, monkeypatch):
    with SyncSession() as db:
        p = Project(org_id=org, name="P", description="d", kind="ai", status="development",
                    workspace_path=str(tmp_path))
        db.add(p)
        db.flush()
        db.add(ProjectFile(project_id=p.id, author="customer", filename="spec.md",
                           content_type="text/markdown", size_bytes=4, data=b"spec"))
        db.add(ProjectFile(project_id=p.id, author="customer", filename="logo.png",
                           content_type="image/png", size_bytes=3, data=b"png"))
        db.commit()
        pid = p.id

        monkeypatch.setattr(tasks, "_build_task_file", lambda *a, **k: ("task", []))
        project = db.get(Project, pid)
        tasks._prepare_runner_inputs(db, project)
        files_dir = tmp_path / ".openvisor" / "files"
        assert (files_dir / "spec.md").read_bytes() == b"spec"
        assert (files_dir / "logo.png").read_bytes() == b"png"

        # Staged fresh per dispatch: a deleted row disappears from the sandbox.
        db.execute(delete(ProjectFile).where(ProjectFile.project_id == pid,
                                             ProjectFile.filename == "logo.png"))
        db.commit()
        tasks._prepare_runner_inputs(db, project)
        assert (files_dir / "spec.md").exists()
        assert not (files_dir / "logo.png").exists()


def test_task_file_lists_imported_files(monkeypatch):
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
    monkeypatch.setattr(tasks, "_project_files_meta", lambda db, project: [
        ("spec.md", "text/markdown", 10)])
    monkeypatch.setattr(tasks.rag, "search", lambda *a, **k: [])
    from app.services import speciality as spec
    monkeypatch.setattr(spec, "deliverable_clause", lambda p: "x")
    monkeypatch.setattr(spec, "knowledge_tags", lambda p: [])
    monkeypatch.setattr(spec, "one_shot_example", lambda p: "")
    from app.agents import pipeline as pl
    monkeypatch.setattr(pl, "_project_context", lambda db, p: "ctx")

    text, _ = tasks._build_task_file(None, _Proj())
    assert "Imported project files" in text
    assert "/workspace/.openvisor/files/spec.md" in text

    monkeypatch.setattr(tasks, "_project_files_meta", lambda db, project: [])
    text2, _ = tasks._build_task_file(None, _Proj())
    assert "Imported project files" not in text2
