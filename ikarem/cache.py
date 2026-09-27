"""Cache interface + in-memory TTL impl + @cached decorator."""

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
