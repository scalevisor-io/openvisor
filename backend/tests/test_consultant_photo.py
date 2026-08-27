"""§consultant photo: the admin's portrait on the public landing.

The landing is a static build shared by every deployment, so the picture is never
a repo asset: an admin uploads it on the Settings page, the platform keeps it in
`brand_asset` and serves it from the public `GET /api/meta/consultant-photo`, and
the landing reveals its photo slots only when that route answers 200. Load-bearing
here: the route 404s until a photo exists (an instance that never uploaded one
renders exactly as before), the bytes are judged by their magic numbers and a
size cap (never the client's content type), and only an admin can change it.
"""
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import BrandAsset, Organization, User
from app.services import consultant_photo
from app.services.images import sniff_image

PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)
JPEG = b"\xff\xd8\xff\xe0" + bytes(64)
WEBP = b"RIFF" + bytes(4) + b"WEBP" + bytes(64)
GIF = b"GIF89a" + bytes(64)

PHOTO = "/api/meta/consultant-photo"
ADMIN_PHOTO = "/api/admin/settings/consultant-photo"


@pytest.fixture(autouse=True)
def _clean_rows():
    def _drop():
        with SyncSession() as db:
            db.execute(delete(BrandAsset).where(BrandAsset.key == consultant_photo.KEY))
            db.commit()
    _drop()
    yield
    _drop()


@pytest.fixture
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _user(role: str):
    email = f"photo-{role}-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "photo-secret-123"
    with SyncSession() as db:
        org = Organization(name=f"Photo {role} Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role=role, email_verified=True))
        org_id = org.id
        db.commit()
    try:
        yield email, pwd
    finally:
        with SyncSession() as db:
            db.execute(delete(User).where(User.org_id == org_id))
            db.execute(delete(Organization).where(Organization.id == org_id))
            db.commit()


@pytest.fixture
def admin():
    yield from _user("admin")


@pytest.fixture
def customer():
    yield from _user("customer")


def _login(client, creds):
    email, pwd = creds
    client.cookies.clear()
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    assert client.post("/api/auth/login", json={"email": email, "password": pwd},
                       headers={"X-CSRF-Token": tok}).status_code == 200
    return {"X-CSRF-Token": tok}


def test_sniffing_reads_the_bytes_not_the_name():
    assert sniff_image(PNG) == "image/png"
    assert sniff_image(JPEG) == "image/jpeg"
    assert sniff_image(WEBP) == "image/webp"
    assert sniff_image(GIF) == "image/gif"
    assert sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert sniff_image(b"") is None


def test_no_photo_is_a_404_for_everyone(client):
    """The landing's photo slots stay hidden on an instance that never uploaded."""
    client.cookies.clear()
    assert client.get(PHOTO).status_code == 404


def test_admin_uploads_replaces_and_removes_the_photo(client, admin):
    """One login covers the whole round trip (the login limiter is per client IP)."""
    h = _login(client, admin)
    assert client.get("/api/admin/settings", headers=h).json()["consultant_photo"] is None

    up = client.put(ADMIN_PHOTO, headers=h, files={"file": ("me.png", PNG, "image/png")})
    assert up.status_code == 200, up.text
    sha = hashlib.sha256(PNG).hexdigest()
    assert up.json()["content_type"] == "image/png"
    assert up.json()["size_bytes"] == len(PNG)
    assert up.json()["sha256"] == sha
    assert up.json()["updated_at"]

    # The public route serves the bytes with a short public cache and a content
    # hash ETag, so a new upload reaches the landing within the minute while an
    # unchanged one revalidates without a byte moving.
    client.cookies.clear()
    pub = client.get(PHOTO)
    assert pub.status_code == 200
    assert pub.headers["content-type"] == "image/png"
    assert pub.content == PNG
    assert pub.headers["cache-control"] == "public, max-age=60"
    assert pub.headers["etag"] == f'"{sha}"'
    assert "access-control-allow-origin" not in pub.headers
    assert client.get(PHOTO, headers={"If-None-Match": f'"{sha}"'}).status_code == 304

    # The Settings page reads the metadata (never the bytes) with the rest.
    h = _login(client, admin)
    listed = client.get("/api/admin/settings", headers=h).json()["consultant_photo"]
    assert listed["sha256"] == sha and listed["content_type"] == "image/png"

    # A replacement is judged by its bytes: a JPEG sent as text/plain is a JPEG.
    rep = client.put(ADMIN_PHOTO, headers=h, files={"file": ("me.txt", JPEG, "text/plain")})
    assert rep.status_code == 200, rep.text
    assert rep.json()["content_type"] == "image/jpeg"
    assert client.get(PHOTO).headers["content-type"] == "image/jpeg"

    # What is not a portrait is refused and the stored photo stays as it was.
    assert client.put(ADMIN_PHOTO, headers=h,
                      files={"file": ("me.gif", GIF, "image/gif")}).status_code == 415
    assert client.put(ADMIN_PHOTO, headers=h,
                      files={"file": ("me.png", b"not an image", "image/png")}).status_code == 415
    too_big = PNG + bytes(consultant_photo.MAX_BYTES)
    assert client.put(ADMIN_PHOTO, headers=h,
                      files={"file": ("me.png", too_big, "image/png")}).status_code == 413
    assert client.get(PHOTO).headers["content-type"] == "image/jpeg"

    # Removing takes the landing back to its photo-less layout; twice is fine.
    assert client.delete(ADMIN_PHOTO, headers=h).status_code == 204
    assert client.get(PHOTO).status_code == 404
    assert client.get("/api/admin/settings", headers=h).json()["consultant_photo"] is None
    assert client.delete(ADMIN_PHOTO, headers=h).status_code == 204


def test_customer_cannot_touch_the_photo(client, customer):
    """The portrait is instance-wide identity; only an admin changes it."""
    h = _login(client, customer)
    assert client.put(ADMIN_PHOTO, headers=h,
                      files={"file": ("me.png", PNG, "image/png")}).status_code == 403
    assert client.delete(ADMIN_PHOTO, headers=h).status_code == 403
    assert client.get(PHOTO).status_code == 404
