"""§chat images: attaching images to chat messages.

The gate is the point: an image may only be uploaded when the project's model is
known to read one, and that is enforced server-side - a UI that got the verdict
wrong still cannot create a message the model will choke on. The rest pins the
claim rules (a message can't adopt someone else's image) and that the worker
inlines the bytes only when the model supports it.
"""
import io

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.core.security import hash_password
from app.models import (AppSetting, ChatImage, Message, ModelEndpoint, Organization,
                        Project, ProjectModelConfig, User)
from app.services import vision

# a real 1x1 PNG - the API sniffs the magic bytes, not the filename
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63fcffff3f0300050001f6a5b8ed0000000049454e44ae426082")


@pytest.fixture(scope="module")
def client(seeded):
    import asyncio

    from fastapi.testclient import TestClient

    from app.core.db import engine
    from app.main import app

    asyncio.run(engine.dispose())
    with TestClient(app) as c:
        # ONE login for the module: rate_limit(request, "login", 20, 900) keys on
        # the client IP, so every test that logs in spends from a budget the whole
        # suite shares - a per-test login here starves later modules.
        _login(c, seeded[3])
        yield c


@pytest.fixture(scope="module")
def seeded():
    with SyncSession() as db:
        org = Organization(name="ChatImg Org", credit_balance=20.0)
        db.add(org)
        db.commit()
        u = User(org_id=org.id, email=f"img-{org.id[:8]}@example.org",
                 password_hash=hash_password("chat-images-secret1"), role="customer",
                 email_verified=True)
        db.add(u)
        p = Project(org_id=org.id, name="P", description="d", kind="chat",
                    status="development", workspace_path="/tmp/img")
        db.add(p)
        db.commit()
        ids = (org.id, u.id, p.id, u.email)
    try:
        yield ids
    finally:
        with SyncSession() as db:
            db.execute(delete(ChatImage).where(ChatImage.project_id == ids[2]))
            db.execute(delete(Message).where(Message.project_id == ids[2]))
            db.execute(delete(ProjectModelConfig).where(ProjectModelConfig.project_id == ids[2]))
            db.execute(delete(Project).where(Project.id == ids[2]))
            db.execute(delete(User).where(User.org_id == ids[0]))
            db.execute(delete(Organization).where(Organization.id == ids[0]))
            db.execute(delete(ModelEndpoint).where(ModelEndpoint.label.like("ImgTest%")))
            db.execute(delete(AppSetting).where(AppSetting.key == vision.DEFAULT_MODEL_IMAGES_KEY))
            db.commit()


def _login(client, email):
    client.get("/api/auth/csrf")
    tok = client.cookies.get("csrf_token") or client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": tok})
    r = client.post("/api/auth/login", json={"email": email, "password": "chat-images-secret1"})
    assert r.status_code == 200, r.text


def _enable_images(pid, supported=True):
    """Point the project at an endpoint with a known verdict, replacing any the
    previous test set (the fixtures are module-scoped now)."""
    with SyncSession() as db:
        db.execute(delete(ModelEndpoint).where(ModelEndpoint.label.like("ImgTest%")))
        ep = ModelEndpoint(label="ImgTest ep", provider="custom",
                           base_url="https://api.example.com/v1", api_key_enc=encrypt("k"),
                           model_name="vision-model-1", supports_images=supported,
                           supports_images_source="probe")
        db.add(ep)
        db.commit()
        db.execute(delete(ProjectModelConfig).where(ProjectModelConfig.project_id == pid))
        db.add(ProjectModelConfig(project_id=pid, endpoint_id=ep.id))
        db.commit()


def test_upload_is_refused_when_the_model_cannot_read_images(client, seeded):
    """The gate, server-side. A UI bug must not be able to create the row."""
    _, _, pid, _ = seeded
    _enable_images(pid, supported=False)
    r = client.post(f"/api/projects/{pid}/chat-images",
                    files=[("files", ("shot.png", io.BytesIO(PNG), "image/png"))])
    assert r.status_code == 409
    assert "can't read images" in r.json()["detail"]
    with SyncSession() as db:
        assert db.execute(select(ChatImage).where(ChatImage.project_id == pid)
                          ).scalars().first() is None


def test_untested_model_is_refused_too(client, seeded):
    _, _, pid, _ = seeded
    # no endpoint config at all → the instance default, which nobody declared
    from sqlalchemy import delete as _delete
    with SyncSession() as db:
        db.execute(_delete(ProjectModelConfig).where(ProjectModelConfig.project_id == pid))
        db.commit()
    r = client.post(f"/api/projects/{pid}/chat-images",
                    files=[("files", ("shot.png", io.BytesIO(PNG), "image/png"))])
    assert r.status_code == 409


def test_upload_then_post_attaches_and_serves(client, seeded):
    _, _, pid, _ = seeded
    _enable_images(pid)

    up = client.post(f"/api/projects/{pid}/chat-images",
                     files=[("files", ("shot.png", io.BytesIO(PNG), "image/png"))])
    assert up.status_code == 201, up.text
    img = up.json()[0]
    assert img["content_type"] == "image/png" and img["size_bytes"] == len(PNG)

    msg = client.post(f"/api/projects/{pid}/messages",
                      json={"thread": "main", "body": "look at this",
                            "image_ids": [img["id"]]})
    assert msg.status_code == 201, msg.text
    assert [i["id"] for i in msg.json()["meta"]["images"]] == [img["id"]]

    got = client.get(f"/api/projects/{pid}/chat-images/{img['id']}")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"


def test_non_image_bytes_are_rejected_whatever_the_header_says(client, seeded):
    _, _, pid, _ = seeded
    _enable_images(pid)
    r = client.post(f"/api/projects/{pid}/chat-images",
                    files=[("files", ("evil.png", io.BytesIO(b"#!/bin/sh\nrm -rf /"),
                                      "image/png"))])
    assert r.status_code == 415


def test_an_image_cannot_be_claimed_twice(client, seeded):
    """Messages are immutable - an id already spent can't move to a later one."""
    _, _, pid, _ = seeded
    _enable_images(pid)
    img = client.post(f"/api/projects/{pid}/chat-images",
                      files=[("files", ("a.png", io.BytesIO(PNG), "image/png"))]).json()[0]
    first = client.post(f"/api/projects/{pid}/messages",
                        json={"thread": "main", "body": "one", "image_ids": [img["id"]]})
    second = client.post(f"/api/projects/{pid}/messages",
                         json={"thread": "main", "body": "two", "image_ids": [img["id"]]})
    assert first.json()["meta"]["images"]
    assert (second.json().get("meta") or {}).get("images") is None


def test_worker_inlines_bytes_only_when_the_model_supports_images(seeded):
    """The last line of defence: even with rows attached, a downgraded model gets
    plain text rather than a payload it would reject."""
    from app.workers import tasks

    _, _, pid, _ = seeded
    _enable_images(pid)
    with SyncSession() as db:
        m = Message(project_id=pid, thread="main", author="customer", body="see this",
                    meta={"images": [{"id": "x"}]})
        db.add(m)
        db.flush()
        db.add(ChatImage(project_id=pid, message_id=m.id, author="customer",
                         filename="a.png", content_type="image/png", size_bytes=len(PNG),
                         data=PNG))
        db.commit()

        content = tasks._message_content(db, m, allow_images=True)
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

        assert tasks._message_content(db, m, allow_images=False) == "see this"
