"""Resilience primitives: timeouts, bulkheads, idempotent writes. Zero deps.

These sit in front of handlers and turn overload into clean 503/429-style
answers instead of hangs and pile-ups:

- ``TimeoutMiddleware(30)`` — a handler that never returns becomes a 503
  with ``Retry-After`` instead of a wedged worker.
- ``ConcurrencyLimitMiddleware(100)`` — bulkhead: past N in-flight
  requests, fail fast with 503 + ``Retry-After: 1``. Uses a non-blocking
  try-acquire, so it never stalls the event loop and works across loops.
- ``IdempotencyMiddleware`` — ``Idempotency-Key`` header turns retried
  POSTs into safe replays (status + body) instead of double charges.
  Backed by any ``CacheBackend``; only 2xx–4xx responses are replayed
  (5xx stays retryable). Concurrent duplicates: last write wins.
"""

from __future__ import annotations

import asyncio
import base64
import threading
from typing import Any

from .http import JSONResponse, Response
from .middleware import Middleware


class TimeoutMiddleware(Middleware):
    def __init__(self, timeout: float = 30.0, retry_after: int = 5):
        self.timeout = timeout
        self.retry_after = retry_after

    async def __call__(self, req: Any, call_next: Any) -> Any:
        try:
            return await asyncio.wait_for(call_next(req), self.timeout)
        except asyncio.TimeoutError:
            resp = JSONResponse({"detail": f"request exceeded {self.timeout:g}s"}, status_code=503)
            resp.headers["retry-after"] = str(self.retry_after)
            return resp


class ConcurrencyLimitMiddleware(Middleware):
    def __init__(self, limit: int = 100, retry_after: int = 1):
        self.limit = limit
        self.retry_after = retry_after
        self._slots = threading.Semaphore(limit)

    async def __call__(self, req: Any, call_next: Any) -> Any:
        if not self._slots.acquire(blocking=False):
            resp = JSONResponse({"detail": "server busy, retry shortly"}, status_code=503)
            resp.headers["retry-after"] = str(self.retry_after)
            return resp
        try:
            return await call_next(req)
        finally:
            self._slots.release()


class IdempotencyMiddleware(Middleware):
    def __init__(
        self,
        cache: Any = None,
        header: str = "idempotency-key",
        methods: tuple[str, ...] = ("POST", "PUT", "PATCH", "DELETE"),
        ttl: int = 3600,
    ):
        from .cache import MemoryCache

        self.cache = cache if cache is not None else MemoryCache()
        self.header = header
        self.methods = {m.upper() for m in methods}
        self.ttl = ttl

    def _key(self, req: Any, client_key: str) -> str:
        return f"idem:{req.method}:{req.path}:{client_key}"

    async def __call__(self, req: Any, call_next: Any) -> Any:
        if req.method not in self.methods:
            return await call_next(req)
        client_key = req.headers.get(self.header, "")
        if not client_key:
            return await call_next(req)  # no key: normal unsafe request
        hit = await self.cache.get(self._key(req, client_key))
        if isinstance(hit, dict) and "status" in hit:
            body = base64.b64decode(hit.get("body_b64", ""))
            return Response(
                body, status_code=int(hit["status"]), media_type=hit.get("media", "application/json")
            )
        resp = await call_next(req)
        if 200 <= resp.status_code < 500 and isinstance(getattr(resp, "body", None), bytes):
            await self.cache.set(
                self._key(req, client_key),
                {
                    "status": resp.status_code,
                    "media": getattr(resp, "media_type", "application/json"),
                    "body_b64": base64.b64encode(resp.body).decode(),
                },
                self.ttl,
            )
        return resp
