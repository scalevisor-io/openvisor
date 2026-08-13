"""Redis pub/sub bridge: API WebSockets subscribe to project channels; both the
API and Celery workers publish events."""
import json

import redis as redis_sync
import redis.asyncio as redis_async

from app.core.config import settings

_async_client: redis_async.Redis | None = None
_sync_client: redis_sync.Redis | None = None


def channel(project_id: str) -> str:
    return f"project:{project_id}"


def get_async_redis() -> redis_async.Redis:
    global _async_client
    if _async_client is None:
        _async_client = redis_async.from_url(settings.redis_url, decode_responses=True)
    return _async_client


def get_sync_redis() -> redis_sync.Redis:
    global _sync_client
    if _sync_client is None:
        _sync_client = redis_sync.from_url(settings.redis_url, decode_responses=True)
    return _sync_client


async def publish_async(project_id: str, event: dict) -> None:
    await get_async_redis().publish(channel(project_id), json.dumps(event, default=str))


def publish_sync(project_id: str, event: dict) -> None:
    get_sync_redis().publish(channel(project_id), json.dumps(event, default=str))
