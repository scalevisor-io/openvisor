"""§chat documents: attaching documents (PDF, Word, Markdown, HTML, text) to chat
messages.

The point is the road a document takes: its text is extracted ONCE at upload
and stored, so any model reads it - no vision gate, unlike images. What this
pins: the sniffing trusts bytes not headers, unreadable documents are refused
with a reason at upload (not discovered by a model that answers as if nothing
was attached), serving is download-only (an uploaded HTML page must never render
under the app origin), the claim rules match images, and the worker folds the
text into the model's view under a newest-first budget.
"""
import io
import zipfile

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.security import hash_password
from app.models import (AppSetting, ChatDocument, Message, ModelEndpoint, Organization,
                        Project, ProjectModelConfig, User)
from app.services import documents, vision

def _pdf(text: str | None) -> bytes:
    """A minimal but well-formed one-page PDF (xref table, trailer, %%EOF) with
    `text` drawn in Helvetica - or nothing at all when `text` is None, which is
    what a scanned page looks like to a parser: a page with no text layer."""
    stream = (f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET" if text is not None else "").encode()
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n"
            ).encode()
    return bytes(out)


PDF = _pdf("Hello document from a PDF")
PDF_EMPTY = _pdf(None)


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


# ------------------------------------------------------------- extraction

def test_sniff_trusts_bytes_and_filename_not_headers():
    assert documents.sniff_document(PDF, "anything.png") == documents.PDF
    assert documents.sniff_document(_docx(["x"]), "brief.docx") == documents.DOCX
    assert documents.sniff_document(b"# Title\n\ntext", "spec.md") == documents.MARKDOWN
    assert documents.sniff_document(b"<!DOCTYPE html><p>x</p>", "page.txt") == documents.HTML
    assert documents.sniff_document(b"a,b\n1,2\n", "rows.csv") == documents.CSV
    assert documents.sniff_document(b"just words", "notes") == documents.PLAIN
    # binary that is not a document: a NUL byte, a zip without a Word body
    assert documents.sniff_document(b"\x89PNG\r\n\x1a\n\x00\x00", "shot.pdf") is None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "x")
    assert documents.sniff_document(buf.getvalue(), "archive.docx") is None


def test_pdf_text_is_extracted_with_page_markers():
    text, pages = documents.extract_text(PDF, documents.PDF)
    assert pages == 1
    assert text.startswith("[page 1]")
    assert "Hello document from a PDF" in text


def test_scanned_pdf_is_refused_with_the_reason():
    with pytest.raises(documents.DocumentError, match="no text layer"):
        documents.extract_text(PDF_EMPTY, documents.PDF)


def test_docx_paragraphs_become_lines():
    text, pages = documents.extract_text(_docx(["Scope", "Deliver a CRM"]), documents.DOCX)
    assert pages is None and text == "Scope\nDeliver a CRM"


def test_html_keeps_visible_text_and_drops_scripts():
    html = (b"<html><head><title>Spec</title><style>p{color:red}</style>"
            b"<script>alert('x')</script></head><body><h1>Login page</h1>"
            b"<p>Users sign in with &amp; email.</p><table><tr><td>a</td><td>b</td></tr>"
            b"</table></body></html>")
    text, _ = documents.extract_text(html, documents.HTML)
    assert "Spec" in text and "Login page" in text and "sign in with & email." in text
    assert "alert" not in text and "color:red" not in text
    assert "\ta\tb" in text


def test_text_is_capped_and_utf8_bom_tolerated():
    text, _ = documents.extract_text(b"\xef\xbb\xbfh\xc3\xa9llo", documents.PLAIN)
    assert text == "héllo"
    big = b"x" * (documents.MAX_TEXT_CHARS + 10)
    text, _ = documents.extract_text(big, documents.PLAIN)
    assert len(text) == documents.MAX_TEXT_CHARS


# ------------------------------------------------------------------- API

@pytest.fixture(scope="module")
def client(seeded):
    import asyncio

    from fastapi.testclient import TestClient

    from app.core.db import engine
    from app.main import app

    asyncio.run(engine.dispose())
    with TestClient(app) as c:
        _login(c, seeded[3])  # one login per module: the login rate limit is shared
        yield c


@pytest.fixture(scope="module")
def seeded():
    with SyncSession() as db:
        org = Organization(name="ChatDoc Org", credit_balance=20.0)
        db.add(org)
        db.commit()
        u = User(org_id=org.id, email=f"doc-{org.id[:8]}@example.org",
                 password_hash=hash_password("chat-docs-secret1"), role="customer",
                 email_verified=True)
        db.add(u)
        p = Project(org_id=org.id, name="P", description="d", kind="chat",
                    status="development", workspace_path="/tmp/doc")
        db.add(p)
        db.commit()
        ids = (org.id, u.id, p.id, u.email)
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(ChatDocument).where(ChatDocument.project_id == ids[2]))
            db.execute(delete(Message).where(Message.project_id == ids[2]))
            db.execute(delete(ProjectModelConfig).where(ProjectModelConfig.project_id == ids[2]))
            db.execute(delete(Project).where(Project.id == ids[2]))
            db.execute(delete(User).where(User.org_id == ids[0]))
            db.execute(delete(Organization).where(Organization.id == ids[0]))
            db.execute(delete(ModelEndpoint).where(ModelEndpoint.label.like("DocTest%")))
            db.execute(delete(AppSetting).where(AppSetting.key == vision.DEFAULT_MODEL_IMAGES_KEY))
            db.commit()


def _login(client, email):
    client.get("/api/auth/csrf")
    tok = client.cookies.get("csrf_token") or client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": tok})
    r = client.post("/api/auth/login", json={"email": email, "password": "chat-docs-secret1"})
    assert r.status_code == 200, r.text


def _upload(client, pid, name, data, ctype="application/octet-stream"):
    return client.post(f"/api/projects/{pid}/chat-documents",
                       files=[("files", (name, io.BytesIO(data), ctype))])


def test_documents_need_no_vision_verdict(client, seeded):
    """The project's model was never checked for images (the default-model flag is
    unset) - an image upload is 409 there, a document goes through: text is text."""
    _, _, pid, _ = seeded
    r = _upload(client, pid, "spec.pdf", PDF, "application/pdf")
    assert r.status_code == 201, r.text
    doc = r.json()[0]
    assert doc["content_type"] == "application/pdf" and doc["pages"] == 1
    assert doc["char_count"] > 0 and doc["truncated"] is False
    assert doc["size_bytes"] == len(PDF)


def test_upload_then_post_attaches_and_serves_as_a_download(client, seeded):
    _, _, pid, _ = seeded
    html = b"<html><body><h1>Brief</h1><script>alert(1)</script></body></html>"
    doc = _upload(client, pid, "brief.html", html, "text/html").json()[0]
    msg = client.post(f"/api/projects/{pid}/messages",
                      json={"thread": "main", "body": "read this brief",
                            "document_ids": [doc["id"]]})
    assert msg.status_code == 201, msg.text
    assert [d["id"] for d in msg.json()["meta"]["documents"]] == [doc["id"]]

    got = client.get(f"/api/projects/{pid}/chat-documents/{doc['id']}")
    assert got.status_code == 200 and got.content == html
    # never an inline render under the app origin: opaque bytes, forced download
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-security-policy"] == "sandbox"
    with SyncSession() as db:
        row = db.get(ChatDocument, doc["id"])
        assert "Brief" in row.text and "alert" not in row.text


def test_a_pdf_downloads_as_a_pdf(client, seeded):
    _, _, pid, _ = seeded
    doc = _upload(client, pid, "spec.pdf", PDF).json()[0]
    got = client.get(f"/api/projects/{pid}/chat-documents/{doc['id']}")
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"].startswith("attachment;")


def test_unsupported_bytes_are_415_whatever_the_name_says(client, seeded):
    _, _, pid, _ = seeded
    r = _upload(client, pid, "spec.pdf", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "application/pdf")
    assert r.status_code == 415
    with SyncSession() as db:
        assert db.execute(select(ChatDocument).where(ChatDocument.filename == "spec.pdf",
                                                     ChatDocument.size_bytes < 100)
                          ).scalars().first() is None


def test_an_unreadable_document_is_refused_at_upload_with_the_reason(client, seeded):
    _, _, pid, _ = seeded
    r = _upload(client, pid, "scan.pdf", PDF_EMPTY, "application/pdf")
    assert r.status_code == 422
    assert "scan.pdf" in r.json()["detail"] and "no text layer" in r.json()["detail"]


def test_a_document_cannot_be_claimed_twice(client, seeded):
    _, _, pid, _ = seeded
    doc = _upload(client, pid, "notes.md", b"# notes\n\nbody", "text/markdown").json()[0]
    first = client.post(f"/api/projects/{pid}/messages",
                        json={"thread": "main", "body": "one", "document_ids": [doc["id"]]})
    second = client.post(f"/api/projects/{pid}/messages",
                         json={"thread": "main", "body": "two", "document_ids": [doc["id"]]})
    assert first.json()["meta"]["documents"]
    assert (second.json().get("meta") or {}).get("documents") is None


# --------------------------------------------------------------- the worker

def _msg_with_doc(db, pid, body, text, filename="spec.md"):
    m = Message(project_id=pid, thread="main", author="customer", body=body,
                meta={"documents": [{"id": "x"}]})
    db.add(m)
    db.flush()
    db.add(ChatDocument(project_id=pid, message_id=m.id, author="customer",
                        filename=filename, content_type="text/markdown",
                        size_bytes=len(text), data=text.encode(), text=text,
                        char_count=len(text), truncated=False, pages=None))
    db.flush()
    return m


def test_worker_folds_the_text_into_the_message_as_a_string(seeded):
    from app.workers import tasks

    _, _, pid, _ = seeded
    with SyncSession() as db:
        m = _msg_with_doc(db, pid, "build this", "# Spec\n\nA CRM with contacts.")
        blocks = tasks._document_blocks(db, [m])
        content = tasks._message_content(db, m, allow_images=False, prefix="[Jane] ",
                                         doc_block=blocks.get(m.id))
        db.rollback()
    assert isinstance(content, str)  # no image: the shape every provider accepts
    assert content.startswith("[Jane] build this\n\n")
    assert 'Attached document "spec.md"' in content
    assert "CUSTOMER-SUPPLIED DATA" in content
    assert "<document name=\"spec.md\">\n# Spec\n\nA CRM with contacts.\n</document>" in content


def test_the_answer_budget_keeps_the_newest_document_in_full(seeded, monkeypatch):
    """Walked newest-first: the document the customer just sent is complete, the
    older one degrades to a stub that still names it."""
    from app.workers import tasks

    _, _, pid, _ = seeded
    monkeypatch.setattr(tasks, "CHAT_DOC_ANSWER_CHARS", 60)
    with SyncSession() as db:
        old = _msg_with_doc(db, pid, "first", "o" * 50, filename="old.md")
        new = _msg_with_doc(db, pid, "second", "n" * 40, filename="new.md")
        blocks = tasks._document_blocks(db, [old, new])
        db.rollback()
    assert "n" * 40 in blocks[new.id]
    assert "o" * 50 not in blocks[old.id]
    assert 'Attached document "old.md"' in blocks[old.id]
    assert "content not included" in blocks[old.id]


def test_a_message_without_documents_is_untouched(seeded):
    from app.workers import tasks

    _, _, pid, _ = seeded
    with SyncSession() as db:
        m = Message(project_id=pid, thread="main", author="customer", body="plain ask")
        db.add(m)
        db.flush()
        assert tasks._document_blocks(db, [m]) == {}
        assert tasks._message_content(db, m, allow_images=True) == "plain ask"
        db.rollback()


# ------------------------------------------------------------ the admin switch

@pytest.fixture
def documents_off():
    """§chat documents switch: the instance kill switch, set the way the admin
    Settings page sets it (an AppSetting flag), cleared afterwards."""
    with SyncSession() as db:
        db.merge(AppSetting(key=documents.DISABLED_KEY, value=True))
        db.commit()
    try:
        yield
    finally:
        with SyncSession() as db:
            db.execute(delete(AppSetting).where(AppSetting.key == documents.DISABLED_KEY))
            db.commit()


def test_the_switch_is_on_by_default_and_public(client):
    assert client.get("/api/settings").json()["chat_documents_enabled"] is True


def test_switched_off_uploads_are_refused_with_the_reason(client, seeded, documents_off):
    _, _, pid, _ = seeded
    assert client.get("/api/settings").json()["chat_documents_enabled"] is False
    r = _upload(client, pid, "notes.md", b"# notes", "text/markdown")
    assert r.status_code == 409
    assert "switched off" in r.json()["detail"]


def test_switched_off_the_model_reads_nothing_and_the_sandbox_gets_nothing(seeded, documents_off,
                                                                           tmp_path):
    """Already-attached documents stay (downloadable) but stop reaching the
    model and the sandbox - the switch governs the feature, not the history."""
    from app.workers import tasks

    _, _, pid, _ = seeded
    with SyncSession() as db:
        m = _msg_with_doc(db, pid, "build this", "# Spec")
        assert tasks._document_blocks(db, [m]) == {}
        assert tasks._message_content(db, m, allow_images=False) == "build this"
        db.rollback()
        project = db.get(Project, pid)
        assert tasks._stage_chat_documents(db, project, tmp_path, None) == []
        assert not (tmp_path / "documents.json").exists()


def test_the_admin_switch_round_trips_through_settings(seeded):
    """PUT /admin/settings stores it, GET /admin/settings and the public
    settings read it back - the same row the upload route checks."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.core.db import engine
    from app.main import app
    from app.core.config import settings as cfg

    asyncio.run(engine.dispose())
    with TestClient(app) as admin:
        admin.get("/api/auth/csrf")
        tok = admin.cookies.get("csrf_token") or admin.get("/api/auth/csrf").json()["csrf_token"]
        admin.headers.update({"X-CSRF-Token": tok})
        r = admin.post("/api/auth/login", json={"email": cfg.admin_email,
                                                 "password": cfg.admin_password})
        assert r.status_code == 200, r.text
        try:
            r = admin.put("/api/admin/settings", json={"chat_documents_disabled": True})
            assert r.status_code == 200, r.text
            assert r.json()["chat_documents_disabled"] is True
            assert admin.get("/api/admin/settings").json()["chat_documents_disabled"] is True
            assert admin.get("/api/settings").json()["chat_documents_enabled"] is False
        finally:
            r = admin.put("/api/admin/settings", json={"chat_documents_disabled": False})
            assert r.status_code == 200, r.text
        assert admin.get("/api/settings").json()["chat_documents_enabled"] is True
