"""In-process function accelerator: `@accelerate()`.

One decorator that makes a hot function fast::

    from ikarem import accelerate

    @accelerate(ttl=60.0, maxsize=1024)
    async def price_list(cur):
        return await db.fetch_all("SELECT ...")  # runs once per minute per cur

What you get over hand-rolled caching (or ``functools.lru_cache``):

- TTL per entry + LRU eviction (bounded memory — plain dicts grow forever).
- singleflight: 100 concurrent identical calls compute ONCE, the rest share
  the result. This is the cache-stampede killer ``cached()`` lacks.
- sync AND async functions (``cached()`` is async-only over a backend).
- exceptions are never cached — errors stay retryable.
- ``fn.cache_info()`` stats + ``fn.cache_clear()``. Thread-safe, loop-aware
  (stale-loop entries are discarded, never awaited), stdlib-only.
  Pass ``clock=`` (seconds fn) for deterministic tests.

When NOT to use it: values shared across processes (use ``cached()`` over
``RedisCache``), request-scoped data (use ``Depends`` caching), or
unbounded-cardinality keys like per-user IDs with no eviction budget —
size the ``maxsize`` or don't decorate.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from collections import OrderedDict
from typing import Any


def _key(args: tuple, kwargs: dict) -> Any:
    """Fast hash path, repr fallback for unhashable arguments."""
    try:
        return ("h", hash((args, tuple(sorted(kwargs.items(), key=lambda kv: kv[0])))))
    except TypeError:
        return ("r", repr((args, sorted(kwargs.items(), key=lambda kv: kv[0]))))


def accelerate(ttl: float = 60.0, maxsize: int = 1024, clock: Any = None) -> Any:
    """Decorate a sync or async function with TTL + LRU + singleflight."""
    if ttl <= 0:
        raise ValueError(f"accelerate needs ttl>0 seconds, got {ttl} (pass e.g. ttl=60.0)")
    if maxsize < 1:
        raise ValueError(f"accelerate needs maxsize>=1, got {maxsize}")
    now = clock or time.monotonic

    def deco(fn: Any) -> Any:
        lock = threading.Lock()
        entries: OrderedDict = OrderedDict()  # key -> [expires_at, result]
        sync_pending: dict = {}  # key -> [threading.Event, slot dict]
        async_pending: dict = {}  # key -> [loop, asyncio.Future]
        stats = {"hits": 0, "misses": 0, "coalesced": 0}

        def _store_locked(key: Any, result: Any, expires: float) -> None:
            entries[key] = [expires, result]
            entries.move_to_end(key)
            while len(entries) > maxsize:
                entries.popitem(last=False)

        def _fresh_locked(key: Any, at: float) -> Any:
            entry = entries.get(key)
            if entry is None:
                return None, False
            if entry[0] <= at:
                del entries[key]
                return None, False
            entries.move_to_end(key)
            return entry[1], True

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def _async_w(*a: Any, **k: Any) -> Any:
                key = _key(a, k)
                loop = asyncio.get_running_loop()
                with lock:
                    result, fresh = _fresh_locked(key, now())
                    if fresh:
                        stats["hits"] += 1
                        return result
                    pend = async_pending.get(key)
                    if pend is not None and pend[0] is loop and not pend[1].done():
                        fut, mine = pend[1], False
                    else:
                        if pend is not None:
                            async_pending.pop(key, None)  # stale loop: never await foreign futures
                        fut = loop.create_future()
                        async_pending[key] = [loop, fut]
                        mine = True
                        stats["misses"] += 1
                if not mine:
                    with lock:
                        stats["coalesced"] += 1
                    return await fut
                try:
                    result = await fn(*a, **k)
                except BaseException as e:
                    with lock:
                        if async_pending.get(key, [None, None])[1] is fut:
                            async_pending.pop(key, None)
                        if not fut.done():
                            fut.set_exception(e)
                    raise
                with lock:
                    _store_locked(key, result, now() + ttl)
                    if async_pending.get(key, [None, None])[1] is fut:
                        async_pending.pop(key, None)
                    if not fut.done():
                        fut.set_result(result)
                return result

            _async_w.cache_info = lambda: {  # type: ignore
                "hits": stats["hits"],
                "misses": stats["misses"],
                "coalesced": stats["coalesced"],
                "size": len(entries),
                "maxsize": maxsize,
                "ttl": ttl,
            }

            def _async_clear() -> None:
                with lock:
                    entries.clear()

            _async_w.cache_clear = _async_clear  # type: ignore
            return _async_w

        @functools.wraps(fn)
        def _sync_w(*a: Any, **k: Any) -> Any:
            key = _key(a, k)
            with lock:
                result, fresh = _fresh_locked(key, now())
                if fresh:
                    stats["hits"] += 1
                    return result
                pend = sync_pending.get(key)
                if pend is not None:
                    ev, slot, mine = pend[0], pend[1], False
                else:
                    ev, slot = threading.Event(), {}
                    sync_pending[key] = [ev, slot]
                    mine = True
                    stats["misses"] += 1
            if not mine:
                with lock:
                    stats["coalesced"] += 1
                ev.wait()
                if "exc" in slot:
                    raise slot["exc"]
                return slot["res"]
            try:
                result = fn(*a, **k)
            except BaseException as e:
                with lock:
                    sync_pending.pop(key, None)
                    slot["exc"] = e
                    ev.set()
                raise
            with lock:
                _store_locked(key, result, now() + ttl)
                sync_pending.pop(key, None)
                slot["res"] = result
                ev.set()
            return result

        _sync_w.cache_info = lambda: {  # type: ignore
            "hits": stats["hits"],
            "misses": stats["misses"],
            "coalesced": stats["coalesced"],
            "size": len(entries),
            "maxsize": maxsize,
            "ttl": ttl,
        }

        def _sync_clear() -> None:
            with lock:
                entries.clear()

        _sync_w.cache_clear = _sync_clear  # type: ignore
        return _sync_w

    return deco
