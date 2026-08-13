"""Post an immutable system message to a project's main chat and push it over
the WS bus. Shared by non-transition events (quotes, ...); status transitions
have their own copy of this logic in services.lifecycle."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, Project
from app.services import events, hub_events


async def post_system_message(db: AsyncSession, project_id: str, body: str) -> Message:
    msg = Message(project_id=project_id, thread="main", author="system", body=body)
    db.add(msg)
    await db.flush()
    hub_events.record(db, await db.get(Project, project_id), "message",
                      hub_events.message_payload(msg))
    await events.publish_async(project_id, {"type": "message", "message": {
        "id": msg.id, "thread": "main", "author": "system", "body": msg.body,
        "emailed": False, "created_at": msg.created_at}})
    return msg
