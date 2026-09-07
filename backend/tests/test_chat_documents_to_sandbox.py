"""§chat documents → sandbox: documents attached to the conversation driving a
run are staged into .openvisor/documents/ - original next to extracted text -
with a manifest and a task block, under the same thread scope and dispatch
window as screenshots (§chat images → sandbox). And, as for screenshots, filing
a request from main must carry the documents down with the words, or a spec
sent where work is described reaches no build."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.core.db import SyncSession
from app.models import ChatDocument, DevRun, Message, Organization, Project, Request, utcnow
from app.services import dev_concurrency
from app.workers import tasks

PDF = b"%PDF-1.4 fake-bytes"


@pytest.fixture
def world():
    with SyncSession() as db:
        org = Organization(name="Doc Org", credit_balance=10.0)
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="Doc", description="d", kind="ai",
                    status="development")
        db.add(p)
        db.flush()
        req = Request(project_id=p.id, title="Build the spec", type="feature",
                      handling="ai", status="open")
        db.add(req)
        db.commit()
        ids = {"org": org.id, "project": p.id, "req": req.id}
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(ChatDocument).where(ChatDocument.project_id == ids["project"]))
            db.execute(delete(Message).where(Message.project_id == ids["project"]))
            db.execute(delete(DevRun).where(DevRun.project_id == ids["project"]))
            db.execute(delete(Request).where(Request.id == ids["req"]))
            db.execute(delete(Project).where(Project.id == ids["project"]))
            db.execute(delete(Organization).where(Organization.id == ids["org"]))
            db.commit()


def _msg_with_doc(db, ids, thread, body, when=None, text="Spec: a CRM", name="spec.pdf"):
    m = Message(project_id=ids["project"], thread=thread, author="customer", body=body,
                meta={"documents": [{"id": "x"}]})
    db.add(m)
    db.flush()
    if when is not None:
        m.created_at = when
    doc = ChatDocument(project_id=ids["project"], message_id=m.id, author="customer",
                       filename=name, content_type="application/pdf", size_bytes=len(PDF),
                       data=PDF, text=text, char_count=len(text), truncated=False, pages=2)
    db.add(doc)
    if when is not None:
        db.flush()
        doc.created_at = when
    db.commit()
    return m.id


def _run(db, ids, predecessor_id=None):
    r = DevRun(project_id=ids["project"], request_id=ids["req"], state="running",
               predecessor_id=predecessor_id)
    db.add(r)
    db.commit()
    return r


def _stage(db, ids, row, tmp_path):
    project = db.get(Project, ids["project"])
    project.dev_request_id = ids["req"]
    dev_concurrency.bind_run(project, row)
    return tasks._stage_chat_documents(db, project, tmp_path, row)


def test_fresh_scoped_run_stages_its_thread_only(world, tmp_path):
    with SyncSession() as db:
        _msg_with_doc(db, world, f"request:{world['req']}", "here is the spec")
        _msg_with_doc(db, world, "main", "unrelated main-thread document")
        row = _run(db, world)
        manifest = _stage(db, world, row, tmp_path)
    assert len(manifest) == 1
    assert manifest[0]["note"].startswith("here is the spec")
    assert manifest[0]["filename"] == "spec.pdf"
    assert (tmp_path / "documents" / "doc-1.pdf").read_bytes() == PDF
    assert (tmp_path / "documents" / "doc-1.pdf.txt").read_text() == "Spec: a CRM"
    assert manifest[0]["text_path"] == ".openvisor/documents/doc-1.pdf.txt"
    assert json.loads((tmp_path / "documents.json").read_text()) == manifest


def test_staging_resets_stale_documents(world, tmp_path):
    (tmp_path / "documents").mkdir()
    (tmp_path / "documents" / "doc-1.pdf").write_bytes(b"stale")
    (tmp_path / "documents.json").write_text("[]")
    with SyncSession() as db:
        row = _run(db, world)
        manifest = _stage(db, world, row, tmp_path)
    assert manifest == []
    assert not (tmp_path / "documents").exists()
    assert not (tmp_path / "documents.json").exists()


def test_chained_run_stages_only_new_documents(world, tmp_path):
    with SyncSession() as db:
        old = utcnow() - timedelta(hours=2)
        _msg_with_doc(db, world, f"request:{world['req']}", "old spec", when=old)
        first = _run(db, world)
        first.created_at = utcnow() - timedelta(hours=1)
        first.state = "failed"
        db.commit()
        _msg_with_doc(db, world, f"request:{world['req']}", "revised spec since the park")
        chained = _run(db, world, predecessor_id=first.id)
        manifest = _stage(db, world, chained, tmp_path)
    assert [e["note"] for e in manifest] == ["revised spec since the park"]


def test_the_task_lists_the_staged_documents(world, tmp_path):
    with SyncSession() as db:
        _msg_with_doc(db, world, f"request:{world['req']}", "build this")
        row = _run(db, world)
        manifest = _stage(db, world, row, tmp_path)
        project = db.get(Project, world["project"])
        task, _ = tasks._build_task_file(db, project, provider="github", documents=manifest)
    assert "## Conversation documents - CUSTOMER-SUPPLIED DATA" in task
    assert "/workspace/.openvisor/documents/doc-1.pdf.txt" in task
    assert "spec.pdf" in task and 'attached to: "build this"' in task


# ------------------------------------------- filing from main carries the documents

def _seed(db, ids, msg_id):
    return tasks._seed_request_thread(db, ids["project"], db.get(Request, ids["req"]),
                                      db.get(Message, msg_id))


def test_filing_a_request_carries_the_main_chat_documents_down(world):
    with SyncSession() as db:
        src = _msg_with_doc(db, world, "main", "build what this spec describes")
        seeded = _seed(db, world, src)
        db.commit()

        carried = db.query(ChatDocument).filter(ChatDocument.message_id == seeded.id).all()
        assert len(carried) == 1
        assert carried[0].data == PDF and carried[0].text == "Spec: a CRM"
        assert carried[0].pages == 2 and carried[0].filename == "spec.pdf"
        assert [d["id"] for d in seeded.meta["documents"]] == [carried[0].id]
        assert "images" not in seeded.meta
        # COPIED, never moved: main still shows what the customer sent
        assert db.query(ChatDocument).filter(ChatDocument.message_id == src).count() == 1


def test_the_carried_document_is_what_the_scoped_run_then_stages(world, tmp_path):
    with SyncSession() as db:
        src = _msg_with_doc(db, world, "main", "build what this spec describes")
        row = _run(db, world)
        assert _stage(db, world, row, tmp_path) == []
        _seed(db, world, src)
        db.flush()
        manifest = _stage(db, world, row, tmp_path)
    assert len(manifest) == 1
    assert (tmp_path / "documents" / "doc-1.pdf.txt").read_text() == "Spec: a CRM"
