"""Global, admin-editable settings backed by the `app_setting` key/value table.

The pause flags gate *new project creation* only (see `api/projects.create_project`)
and are surfaced to every client through `GET /api/meta/config`. They never touch the
request/edit flow of an already-created project.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import AppSetting

# §legal identity: the operating company's legal name and registered address,
# admin-editable so a deployment can name itself correctly in its Privacy policy
# and Terms of service without rebuilding the landing image. Empty (the default)
# means "use whatever the landing was built with" (site.yml `legal.entity`).
LEGAL_NAME = "legal_business_name"
LEGAL_ADDRESS = "legal_business_address"

# §consultant identity: who the practice is, as two fields rather than one
# string - a last name is not "everything after the first space" in every
# culture, and the first name alone is what the chat and the emails say.
# Admin-editable (Settings -> Consultant) so a deployment can name its
# consultant without rebuilding the landing OR redeploying the API; empty
# (the default) falls back to the CONSULTANT_NAME env var.
CONSULTANT_FIRST_NAME = "consultant_first_name"
CONSULTANT_LAST_NAME = "consultant_last_name"

PAUSE_AI = "pause_ai_deposits"
PAUSE_DIRECT = "pause_direct_deposits"
PAUSE_AUTO_DEV = "pause_auto_dev_deposits"
PAUSE_CHAT = "pause_chat_deposits"

# maps a Project.kind to the flag that pauses its creation
_KIND_FLAG = {"ai": PAUSE_AI, "direct_quote": PAUSE_DIRECT, "auto_dev": PAUSE_AUTO_DEV,
              "chat": PAUSE_CHAT}


async def get_deposit_pause(db: AsyncSession) -> dict[str, bool]:
    """Return the deposit-pause flags, defaulting missing rows to False - except
    chat, which is opt-in (paused until the admin explicitly enables it): chat
    exposes the knowledge base conversationally, so each instance owner decides."""
    rows = (
        await db.execute(select(AppSetting).where(
            AppSetting.key.in_([PAUSE_AI, PAUSE_DIRECT, PAUSE_AUTO_DEV, PAUSE_CHAT])))
    ).scalars().all()
    stored = {r.key: bool(r.value) for r in rows}
    return {
        PAUSE_AI: stored.get(PAUSE_AI, False),
        PAUSE_DIRECT: stored.get(PAUSE_DIRECT, False),
        PAUSE_AUTO_DEV: stored.get(PAUSE_AUTO_DEV, False),
        PAUSE_CHAT: stored.get(PAUSE_CHAT, True),
    }


async def set_deposit_pause(
    db: AsyncSession, *, pause_ai: bool | None = None, pause_direct: bool | None = None,
    pause_auto_dev: bool | None = None, pause_chat: bool | None = None,
) -> None:
    """Partial update: only the flags passed (not None) are written. Caller commits."""
    updates = {PAUSE_AI: pause_ai, PAUSE_DIRECT: pause_direct, PAUSE_AUTO_DEV: pause_auto_dev,
               PAUSE_CHAT: pause_chat}
    for key, value in updates.items():
        if value is None:
            continue
        row = await db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=bool(value)))
        else:
            row.value = bool(value)


def is_kind_paused(flags: dict[str, bool], kind: str) -> bool:
    """True if new deposits of this project kind are currently paused."""
    flag = _KIND_FLAG.get(kind)
    return bool(flag and flags.get(flag))


# ---- generic sync accessors (Celery workers; the pause flags above stay async) ----

def get_setting_sync(db: Session, key: str, default=None):
    """Read one AppSetting value in a sync (Celery) session, defaulting missing rows."""
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting_sync(db: Session, key: str, value) -> None:
    """Upsert one AppSetting value in a sync (Celery) session. Caller commits."""
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


# ---- generic async accessors (API routes) ----

async def get_flag(db: AsyncSession, key: str) -> bool:
    """One boolean AppSetting, false when the row was never written."""
    row = await db.get(AppSetting, key)
    return bool(row.value) if row is not None else False


async def set_flag(db: AsyncSession, key: str, value: bool) -> None:
    """Upsert one boolean AppSetting. Caller commits."""
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=bool(value)))
    else:
        row.value = bool(value)


async def get_value(db: AsyncSession, key: str, default=None):
    """Read one AppSetting value (any JSON), defaulting a missing row."""
    row = await db.get(AppSetting, key)
    return row.value if row is not None else default


async def set_value(db: AsyncSession, key: str, value) -> None:
    """Upsert one AppSetting value (any JSON). Caller commits."""
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


# ---- §legal identity ----

async def get_legal_identity(db: AsyncSession) -> dict[str, str]:
    """The legal name/address pair, both empty strings when never set."""
    return {
        "legal_name": str(await get_value(db, LEGAL_NAME, "") or ""),
        "legal_address": str(await get_value(db, LEGAL_ADDRESS, "") or ""),
    }


async def set_legal_identity(db: AsyncSession, *, name: str | None = None,
                             address: str | None = None) -> None:
    """Partial update, trimmed; an empty string clears the field back to the
    landing's built-in value. Caller commits."""
    if name is not None:
        await set_value(db, LEGAL_NAME, name.strip())
    if address is not None:
        await set_value(db, LEGAL_ADDRESS, address.strip())


# ---- §consultant identity ----

async def get_consultant_identity(db: AsyncSession) -> dict[str, str]:
    """The stored first/last name pair, both empty strings when never set.
    Empty means "fall back to CONSULTANT_NAME" - resolving that fallback is
    `services.brand`, so every reader gets the same answer."""
    return {
        "consultant_first_name": str(await get_value(db, CONSULTANT_FIRST_NAME, "") or ""),
        "consultant_last_name": str(await get_value(db, CONSULTANT_LAST_NAME, "") or ""),
    }


async def set_consultant_identity(db: AsyncSession, *, first: str | None = None,
                                  last: str | None = None) -> None:
    """Partial update, trimmed; an empty string clears the field back to the
    env default. Caller commits."""
    if first is not None:
        await set_value(db, CONSULTANT_FIRST_NAME, first.strip())
    if last is not None:
        await set_value(db, CONSULTANT_LAST_NAME, last.strip())


def get_consultant_identity_sync(db: Session) -> dict[str, str]:
    """The same pair in a sync (Celery) session - `services.brand` reads it
    through here when it refreshes its cache."""
    return {
        "consultant_first_name": str(get_setting_sync(db, CONSULTANT_FIRST_NAME, "") or ""),
        "consultant_last_name": str(get_setting_sync(db, CONSULTANT_LAST_NAME, "") or ""),
    }
