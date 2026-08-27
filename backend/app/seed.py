"""Idempotent startup seed (PROMPT §7): the admin account and the built-in
knowledge bases (§KB). Runs on every api/migrate boot; safe to re-run."""
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.db import SyncSession
from app.core.security import hash_password
from app.models import Tool, KnowledgeBase, Membership, Organization, User
from app.services import donsetch, websearch

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("seed")

# The two built-in, non-removable knowledge bases: (kind, name, sort_order).
_BUILTIN_KBS = [
    ("local", "Platform knowledge base (/knowledge)", 0),
    ("context7", "Context7 - library documentation", 1),
]

def seed_admin() -> None:
    with SyncSession() as db:
        existing = db.execute(
            select(User).where(User.email == settings.admin_email)).scalar_one_or_none()
        if existing:
            if existing.role != "admin":
                existing.role = "admin"
                db.commit()
            log.info("admin %s already present", settings.admin_email)
            return
        org = Organization(name="openvisor", type="organization", company_name="openvisor")
        db.add(org)
        db.flush()
        user = User(org_id=org.id, email=settings.admin_email,
                    password_hash=hash_password(settings.admin_password),
                    role="admin", email_verified=True)
        db.add(user)
        db.flush()
        db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
        db.commit()
        log.info("seeded admin %s", settings.admin_email)


def seed_knowledge_bases() -> None:
    """Ensure the built-in local + Context7 knowledge bases exist. Idempotent
    (get-or-create per kind) and tolerant of a concurrent double-seed thanks to
    the partial-unique index - never duplicates a singleton kind on restart."""
    with SyncSession() as db:
        for kind, name, order in _BUILTIN_KBS:
            existing = db.execute(select(KnowledgeBase).where(
                KnowledgeBase.kind == kind)).scalar_one_or_none()
            if existing:
                continue
            db.add(KnowledgeBase(kind=kind, name=name, enabled=True,
                                 is_removable=False, sort_order=order))
            try:
                db.commit()
                log.info("seeded knowledge base %s", kind)
            except IntegrityError:
                db.rollback()  # another booting container won the race - fine

_BUILTIN_TOOLS = [
    # (slug, name, kind, url) - disabled until the admin configures a key.
    ("github", "GitHub", "github", "https://api.githubcopilot.com/mcp/"),
    ("gitlab", "GitLab", "gitlab", "https://gitlab.com/api/v4/mcp"),
    # §web research: keyless, so it needs no configuring - only enabling. `url`
    # is the sidecar BASE; the served path carries the enabled capabilities.
    (donsetch.SLUG, "Web research (DonSeTch)", donsetch.KIND,
     settings.donsetch_mcp_url),
    # Keyed web-search providers: one row per provider, pointing at that
    # provider's route on the websearch-mcp sidecar. Disabled until a key passes
    # the server-side probe.
    *((websearch.tool_slug(p), websearch.PROVIDER_NAMES[p], websearch.KIND,
       websearch.endpoint(settings.websearch_mcp_url, p))
      for p in websearch.PROVIDERS),
]


def seed_tools() -> None:
    """Ensure the built-in MCP tools exist (§Tools): GitHub, GitLab, the §web
    research engine and one row per keyed web-search provider. Idempotent per
    slug; the unique index absorbs a concurrent double-seed. The GitLab row's URL
    is editable for self-hosted instances (https://<host>/api/v4/mcp)."""
    with SyncSession() as db:
        for slug, name, kind, url in _BUILTIN_TOOLS:
            if db.execute(select(Tool).where(Tool.slug == slug)).scalar_one_or_none():
                continue
            params = None
            if kind == donsetch.KIND:
                params = {"capabilities": list(donsetch.DEFAULT_CAPABILITIES)}
            elif kind == websearch.KIND:
                # The provider is the row's identity for verification and for the
                # sidecar route; derive nothing from the slug at call sites.
                params = {"provider": slug.removeprefix("websearch_")}
            db.add(Tool(slug=slug, name=name, kind=kind, url=url, enabled=False,
                        params=params))
            try:
                db.commit()
                log.info("seeded tool %s", slug)
            except IntegrityError:
                db.rollback()


def seed() -> None:
    seed_admin()
    seed_knowledge_bases()
    seed_tools()


if __name__ == "__main__":
    seed()
