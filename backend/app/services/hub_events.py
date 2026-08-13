"""Hub pass-through event outbox writers (§P1). `record(db, project, etype,
payload)` inserts a HubProjectEvent row in the CALLER's session/transaction -
never at the Redis publish layer - so the outbox row commits or rolls back with
the state it mirrors. A no-op for non-hub projects, so every call site is safe
unconditionally. Works with both Session and AsyncSession (db.add is sync on
both). The Beat push job lives in workers/hub.py."""
from app.models import HubProjectEvent, Message, Project

_BODY_CAP = 4000


def record(db, project: Project | None, etype: str, payload: dict) -> None:
    if project is None or getattr(project, "source", None) != "hub":
        return
    db.add(HubProjectEvent(project_id=project.id, hub_ref=project.hub_ref,
                           etype=etype, payload=payload))


def message_payload(msg: Message) -> dict:
    return {"id": msg.id, "thread": msg.thread, "author": msg.author,
            "body": (msg.body or "")[:_BODY_CAP], "meta": msg.meta,
            "created_at": msg.created_at.isoformat() if msg.created_at else None}
