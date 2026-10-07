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
- ``SpikeManager`` — the intelligent bulkhead: AIMD adaptive concurrency
  (grows on fast responses, shrinks on slow ones) instead of a guessed
  fixed N; bounded queue instead of instant 503; exemptions so health
  probes are never shed; ``snapshot()`` for dashboards.

A network load balancer is deliberately NOT here: spreading traffic
across machines is nginx / cloud-LB / k8s territory. SpikeManager is
the per-process half — admit, queue, shed, adapt — which is the part a
framework can do honestly.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
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


class SpikeManager(Middleware):
    """Adaptive spike shield: AIMD limit + bounded queue + exemptions.

    Fixed bulkheads guess N once and fail fast. This one learns::

        app.use(SpikeManager(initial=100, max_limit=500, target_latency=0.2,
                             queue_timeout=0.5, exempt_paths=("/healthz", "/readyz")))

    - admit while in-flight < limit; else wait up to ``queue_timeout``
      for a slot (polls without blocking the event loop); else 503 +
      ``Retry-After``. ``queue_timeout=0`` reproduces fail-fast bulkheads.
    - every completed request moves the limit: +1 when faster than
      ``target_latency`` (up to ``max_limit``), x0.9 when slower (down to
      ``min_limit``). Sustained overload shrinks the gate; recovery
      reopens it one slot per fast request.
    - ``exempt_paths`` + ``exempt(req)`` traffic bypasses the gate
      entirely: shed health probes and the orchestrator kills the
      instance mid-spike. Probes are cheap and rare; bypass is the safe
      default. Never count exempt traffic against the limit.
    - ``snapshot()`` returns ``{limit, in_flight, admitted, shed,
      queued, avg_latency}`` for dashboards. Thread-safe, loop-agnostic
      (threading primitives only), stdlib-only. Pass ``clock=`` (seconds
      fn) for deterministic tests; queue waits always use monotonic time.
    """

    _POLL = 0.005

    def __init__(
        self,
        initial: int = 100,
        min_limit: int = 10,
        max_limit: int = 1000,
        target_latency: float = 0.2,
        queue_timeout: float = 0.0,
        retry_after: int = 1,
        exempt_paths: tuple[str, ...] = (),
        exempt: Any = None,
        clock: Any = None,
    ):
        if min_limit < 1:
            raise ValueError(f"SpikeManager needs min_limit>=1, got {min_limit}")
        if max_limit < min_limit:
            raise ValueError(
                f"SpikeManager needs max_limit>=min_limit, got max_limit={max_limit} min_limit={min_limit}"
            )
        if not (min_limit <= initial <= max_limit):
            raise ValueError(
                f"SpikeManager needs min_limit<=initial<=max_limit, got {initial} "
                f"outside [{min_limit}, {max_limit}]"
            )
        if target_latency <= 0:
            raise ValueError(f"SpikeManager needs target_latency>0, got {target_latency}")
        if queue_timeout < 0:
            raise ValueError(f"SpikeManager needs queue_timeout>=0, got {queue_timeout}")
        for p in exempt_paths:
            if not isinstance(p, str):
                raise ValueError(f"SpikeManager exempt_paths must be strings, got {p!r}")
        if exempt is not None and not callable(exempt):
            raise ValueError("SpikeManager exempt must be a callable(req) -> bool or None")
        self._limit = float(initial)
        self._min = float(min_limit)
        self._max = float(max_limit)
        self._target = target_latency
        self._queue_timeout = queue_timeout
        self._retry_after = retry_after
        self._exempt_paths = tuple(exempt_paths)
        self._exempt = exempt
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._in_flight = 0
        self._admitted = 0
        self._shed = 0
        self._queued = 0
        self._avg_lat: float | None = None

    def _is_exempt(self, req: Any) -> bool:
        if self._exempt_paths and getattr(req, "path", None) in self._exempt_paths:
            return True
        if self._exempt is not None:
            try:
                return bool(self._exempt(req))
            except Exception:
                return False
        return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "limit": int(self._limit),
                "in_flight": self._in_flight,
                "admitted": self._admitted,
                "shed": self._shed,
                "queued": self._queued,
                "avg_latency": self._avg_lat,
            }

    def _shed_response(self) -> Any:
        resp = JSONResponse({"detail": "server busy, retry shortly"}, status_code=503)
        resp.headers["retry-after"] = str(self._retry_after)
        return resp

    async def _await_slot(self, timeout: float) -> bool:
        """Poll for a slot without ever blocking the event loop."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._in_flight < self._limit:
                    self._in_flight += 1
                    self._admitted += 1
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self._POLL, remaining))

    def _adapt(self, latency: float) -> None:
        with self._lock:
            if latency > self._target:
                self._limit = max(self._min, self._limit * 0.9)
            else:
                self._limit = min(self._max, self._limit + 1.0)
            avg = self._avg_lat
            self._avg_lat = latency if avg is None else 0.9 * avg + 0.1 * latency

    async def __call__(self, req: Any, call_next: Any) -> Any:
        if self._is_exempt(req):
            with self._lock:
                self._admitted += 1
            return await call_next(req)
        queued = False
        with self._lock:
            if self._in_flight < self._limit:
                self._in_flight += 1
                self._admitted += 1
            elif self._queue_timeout <= 0:
                self._shed += 1
                return self._shed_response()
            else:
                queued = True
        if queued:
            with self._lock:
                self._queued += 1
            try:
                if not await self._await_slot(self._queue_timeout):
                    with self._lock:
                        self._shed += 1
                    return self._shed_response()
            finally:
                with self._lock:
                    self._queued -= 1
        start = self._clock()
        try:
            return await call_next(req)
        finally:
            self._adapt(self._clock() - start)
            with self._lock:
                self._in_flight -= 1


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
