"""Global, admin-editable settings backed by the `app_setting` key/value table.

The pause flags gate *new project creation* only (see `api/projects.create_project`)
and are surfaced to every client through `GET /api/meta/config`. They never touch the
request/edit flow of an already-created project.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import AppSetting

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
