"""rate_limit hardening (deps.rate_limit): fails OPEN on a Redis outage instead of
500-ing, and self-heals a counter that lost its TTL (a crash between INCR and
EXPIRE would otherwise wedge it at a permanent 429). Driven directly as a coroutine
with a fake async Redis - no DB, no HTTP - so the shared helper's semantics (which
login/signup also depend on) stay pinned."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import deps


def _req():
    # rate_limit only reads request.client when identity is None; we always pass an
    # explicit identity here, so a placeholder request is enough.
    return SimpleNamespace(client=None)


class _FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def incr(self, k):
        self.counts[k] = self.counts.get(k, 0) + 1
        return self.counts[k]

    async def ttl(self, k):
        return self.ttls.get(k, -1)

    async def expire(self, k, s):
        self.ttls[k] = s
        self.expire_calls.append((k, s))


class _DownRedis:
    async def incr(self, k):
        raise ConnectionError("redis down")

    async def ttl(self, k):  # pragma: no cover - never reached (incr fails first)
        raise ConnectionError("redis down")

    async def expire(self, k, s):  # pragma: no cover
        raise ConnectionError("redis down")


def test_fails_open_when_redis_down(monkeypatch):
    monkeypatch.setattr(deps, "get_async_redis", lambda: _DownRedis())
    # Must NOT raise (fail open) even with the limit set to 0 - a Redis outage can
    # never turn the limiter into a hard 500.
    asyncio.run(deps.rate_limit(_req(), "eval", 0, 60, identity="hub:1"))


def test_sets_ttl_on_first_hit(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(deps, "get_async_redis", lambda: fake)
    asyncio.run(deps.rate_limit(_req(), "eval", 5, 90, identity="hub:1"))
    assert fake.expire_calls == [("rl:eval:hub:1", 90)]


def test_reexpires_orphaned_ttl_less_key(monkeypatch):
    fake = _FakeRedis()
    # Simulate a counter left without a TTL (crash between INCR and EXPIRE): it
    # already holds a value and ttl() reports -1.
    fake.counts["rl:eval:hub:1"] = 1
    fake.ttls["rl:eval:hub:1"] = -1
    monkeypatch.setattr(deps, "get_async_redis", lambda: fake)
    # Next call bumps to 2 (not the first hit) but must still re-arm the expiry.
    asyncio.run(deps.rate_limit(_req(), "eval", 5, 90, identity="hub:1"))
    assert ("rl:eval:hub:1", 90) in fake.expire_calls


def test_raises_429_over_limit(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(deps, "get_async_redis", lambda: fake)
    asyncio.run(deps.rate_limit(_req(), "eval", 2, 90, identity="hub:1"))
    asyncio.run(deps.rate_limit(_req(), "eval", 2, 90, identity="hub:1"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(deps.rate_limit(_req(), "eval", 2, 90, identity="hub:1"))
    assert ei.value.status_code == 429
