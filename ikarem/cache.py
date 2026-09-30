"""Cache interface + in-memory TTL impl + Redis impl + @cached decorator.

Single-process ceiling: ``MemoryCache``, ``RateLimitMiddleware`` buckets,
``Room`` pub/sub, and ``IdempotencyMiddleware``'s default cache live in
process memory. They are correct behind one worker and wrong behind eight.
For multi-process deploys pass a shared backend (``RedisCache``) to
``IdempotencyMiddleware(cache=...)`` / ``cached(...)`` instead of the
default — same ``CacheBackend`` interface, no per-request reflection.
"""

from __future__ import annotations

import abc
import functools
import time
from typing import Any


class CacheBackend(abc.ABC):
    @abc.abstractmethod
    async def get(self, key: str) -> Any | None: ...
    @abc.abstractmethod
    async def set(self, key: str, value: Any, ttl: int = 60) -> None: ...
    @abc.abstractmethod
    async def delete(self, key: str) -> None: ...


class MemoryCache(CacheBackend):
    """Bounded in-memory TTL cache: expired entries are swept on write,
    and the oldest-expiring entries are evicted past maxsize. Never grows
    without bound, even under adversarial key cardinality."""

    def __init__(self, maxsize: int = 10_000) -> None:
        self._d: dict[str, tuple[Any, float]] = {}
        self.maxsize = maxsize

    async def get(self, key: str) -> Any | None:
        v = self._d.get(key)
        if not v:
            return None
        val, exp = v
        if exp < time.time():
            self._d.pop(key, None)
            return None
        return val

    async def set(self, key: str, value: Any, ttl: int = 60) -> None:
        now = time.time()
        self._d[key] = (value, now + ttl)
        if len(self._d) > self.maxsize:
            # sweep expired first (common case: all TTL'd anyway)
            expired = [k for k, (_, exp) in self._d.items() if exp < now]
            for k in expired:
                self._d.pop(k, None)
            # still over: evict soonest-expiring (closest to worthless)
            while len(self._d) > self.maxsize:
                oldest = min(self._d, key=lambda k: self._d[k][1])
                self._d.pop(oldest, None)

    async def delete(self, key: str) -> None:
        self._d.pop(key, None)


class RedisCache(CacheBackend):
    """Shared Redis backend for multi-process deploys (lazy ``redis`` import).

    Values are JSON-serialized, so only JSON-compatible payloads survive
    (dicts/lists/str/numbers — exactly what idempotency replay stores).
    Pass an already-connected ``redis.asyncio.Redis`` as ``client=`` in
    tests to avoid needing a server.
    """

    def __init__(self, url: str = "redis://localhost:6379/0", prefix: str = "ikarem:", client: Any = None):
        self.url = url
        self.prefix = prefix
        self._client = client

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    async def _client_or_connect(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from redis import asyncio as aioredis
        except ImportError as e:
            raise RuntimeError("pip install ikarem[redis] to use RedisCache (needs redis)") from e
        self._client = aioredis.from_url(self.url, decode_responses=False)
        return self._client

    async def get(self, key: str) -> Any | None:
        import json as _json

        client = await self._client_or_connect()
        raw = await client.get(self._key(key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return _json.loads(raw)
        except Exception:
            return raw

    async def set(self, key: str, value: Any, ttl: int = 60) -> None:
        import json as _json

        client = await self._client_or_connect()
        try:
            body = _json.dumps(value)
        except TypeError as e:
            raise ValueError(f"RedisCache only stores JSON-compatible values for key {key!r}: {e}") from e
        await client.setex(self._key(key), ttl, body)

    async def delete(self, key: str) -> None:
        client = await self._client_or_connect()
        await client.delete(self._key(key))


def cached(cache: CacheBackend, ttl: int = 60, key_prefix: str = ""):
    def deco(fn: Any) -> Any:
        @functools.wraps(fn)
        async def _w(*a: Any, **k: Any) -> Any:
            import inspect

            key = f"{key_prefix}:{fn.__name__}:{a!r}:{k!r}"
            hit = await cache.get(key)
            if hit is not None:
                return hit
            res = fn(*a, **k)
            if inspect.isawaitable(res):
                res = await res
            await cache.set(key, res, ttl)
            return res

        return _w

    return deco


def _iscoro(fn: Any) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)
