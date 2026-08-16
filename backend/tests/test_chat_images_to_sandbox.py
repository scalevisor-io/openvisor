"""§chat images → sandbox: screenshots attached to the conversation driving a
run are staged into .openvisor/images/ with a manifest the runner attaches to
its first message. Vision-gated per dispatch; thread scope mirrors §steering
scope (a scoped request stages its OWN thread, MVP adds main); a chained run
stages only images its predecessor never saw; staging always resets first."""
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.core.db import SyncSession
from app.models import ChatImage, DevRun, Message, Organization, Project, Request, utcnow
from app.services import dev_concurrency, vision
from app.workers import tasks

PNG = b"\x89PNG\r\n\x1a\nfake-bytes"


@pytest.fixture
def world():
    with SyncSession() as db:
        org = Organization(name="Img Org", credit_balance=10.0)
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="Img", description="d", kind="ai",
                    status="development")
        db.add(p)
        db.flush()
        req = Request(project_id=p.id, title="Fix the header", type="bug",
                      handling="ai", status="open")
        db.add(req)
        db.commit()
        ids = {"org": org.id, "project": p.id, "req": req.id}
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(ChatImage).where(ChatImage.project_id == ids["project"]))
            db.execute(delete(Message).where(Message.project_id == ids["project"]))
            db.execute(delete(DevRun).where(DevRun.project_id == ids["project"]))
            db.execute(delete(Request).where(Request.id == ids["req"]))
            db.execute(delete(Project).where(Project.id == ids["project"]))
            db.execute(delete(Organization).where(Organization.id == ids["org"]))
            db.commit()


def _msg_with_image(db, ids, thread, body, when=None, ctype="image/png"):
    m = Message(project_id=ids["project"], thread=thread, author="admin", body=body)
    db.add(m)
    db.flush()
    if when is not None:
        m.created_at = when
    img = ChatImage(project_id=ids["project"], message_id=m.id, author="admin",
                    filename="s.png", content_type=ctype, size_bytes=len(PNG), data=PNG)
    db.add(img)
    if when is not None:
        db.flush()
        img.created_at = when
    db.commit()
    return m.id


def _run(db, ids, predecessor_id=None):
    r = DevRun(project_id=ids["project"], request_id=ids["req"], state="running",
               predecessor_id=predecessor_id)
    db.add(r)
    db.commit()
    return r


def _stage(db, ids, row, tmp_path, enabled=True, monkeypatch=None):
    monkeypatch.setattr(vision, "project_image_support_sync",
                        lambda _db, _p: {"enabled": enabled})
    project = db.get(Project, ids["project"])
    project.dev_request_id = ids["req"]
    dev_concurrency.bind_run(project, row)
    return tasks._stage_chat_images(db, project, tmp_path, row)


def test_fresh_scoped_run_stages_its_thread_only(world, tmp_path, monkeypatch):
    with SyncSession() as db:
        _msg_with_image(db, world, f"request:{world['req']}", "here is the broken header")
        _msg_with_image(db, world, "main", "unrelated main-thread screenshot")
        row = _run(db, world)
        manifest = _stage(db, world, row, tmp_path, monkeypatch=monkeypatch)
    assert len(manifest) == 1
    assert manifest[0]["note"].startswith("here is the broken header")
    assert (tmp_path / "images" / "img-1.png").read_bytes() == PNG
    assert json.loads((tmp_path / "images.json").read_text()) == manifest


def test_vision_disabled_stages_nothing_and_resets_stale(world, tmp_path, monkeypatch):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "img-1.png").write_bytes(b"stale")
    (tmp_path / "images.json").write_text("[]")
    with SyncSession() as db:
        _msg_with_image(db, world, f"request:{world['req']}", "screenshot")
        row = _run(db, world)
        manifest = _stage(db, world, row, tmp_path, enabled=False, monkeypatch=monkeypatch)
    assert manifest == []
    assert not (tmp_path / "images").exists()
    assert not (tmp_path / "images.json").exists()


def test_chained_run_stages_only_new_images(world, tmp_path, monkeypatch):
    with SyncSession() as db:
        old = utcnow() - timedelta(hours=2)
        _msg_with_image(db, world, f"request:{world['req']}", "old screenshot", when=old)
        first = _run(db, world)
        first.created_at = utcnow() - timedelta(hours=1)
        first.state = "failed"  # parked - one ACTIVE run per request (uq_devrun_active_request)
        db.commit()
        _msg_with_image(db, world, f"request:{world['req']}", "new screenshot since the park")
        chained = _run(db, world, predecessor_id=first.id)
        manifest = _stage(db, world, chained, tmp_path, monkeypatch=monkeypatch)
    assert [e["note"] for e in manifest] == ["new screenshot since the park"]


def test_unknown_content_type_is_skipped(world, tmp_path, monkeypatch):
    with SyncSession() as db:
        _msg_with_image(db, world, f"request:{world['req']}", "a tiff", ctype="image/tiff")
        row = _run(db, world)
        manifest = _stage(db, world, row, tmp_path, monkeypatch=monkeypatch)
    assert manifest == []
