"""Security middleware: CORS, headers, rate-limit. Industry defaults."""

from __future__ import annotations

import time
from typing import Any

from .http import JSONResponse
from .middleware import Middleware


class CORSMiddleware(Middleware):
    def __init__(
        self,
        allow_origins: list[str] | None = None,
        allow_methods: list[str] | None = None,
        allow_headers: list[str] | None = None,
    ):
        self.origins = allow_origins or ["*"]
        self.methods = allow_methods or ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
        self.headers = allow_headers or ["authorization", "content-type"]

    async def __call__(self, req: Any, call_next: Any) -> Any:
        if req.method == "OPTIONS":
            from .http import Response

            return Response(
                b"",
                204,
                {
                    "access-control-allow-origin": ", ".join(self.origins),
                    "access-control-allow-methods": ", ".join(self.methods),
                    "access-control-allow-headers": ", ".join(self.headers),
                },
            )
        resp = await call_next(req)
        resp.headers["access-control-allow-origin"] = ", ".join(self.origins)
        return resp


class SecurityHeadersMiddleware(Middleware):
    """Secure-by-default headers. CSP is opt-in (a wrong default breaks
    apps that inline scripts) — pass e.g. ``content_security_policy=
    "default-src 'self'"`` when ready."""

    def __init__(self, content_security_policy: str | None = None):
        self.csp = content_security_policy

    async def __call__(self, req: Any, call_next: Any) -> Any:
        resp = await call_next(req)
        resp.headers.setdefault("x-content-type-options", "nosniff")
        resp.headers.setdefault("x-frame-options", "DENY")
        resp.headers.setdefault("referrer-policy", "no-referrer")
        if self.csp:
            resp.headers.setdefault("content-security-policy", self.csp)
        return resp


class TrustedHostMiddleware(Middleware):
    """Reject Host headers outside an allowlist (cache poisoning,
    password-reset link theft, host-header injection).

    Exact names plus leading-dot wildcards: ``TrustedHostMiddleware(
    ["example.com", ".example.com"])``. Rejected requests get 400 without
    touching handlers. Health probes hitting by IP need listing too."""

    def __init__(self, allowed_hosts: list[str] | None = None):
        self.allowed = [h.lower() for h in (allowed_hosts or ["*"])]

    def _ok(self, host: str) -> bool:
        host = host.split(":")[0].lower()
        for rule in self.allowed:
            if rule == "*":
                return True
            if rule.startswith("."):
                if host == rule[1:] or host.endswith(rule):
                    return True
            elif host == rule:
                return True
        return False

    async def __call__(self, req: Any, call_next: Any) -> Any:
        host = req.headers.get("host", "")
        if not self._ok(host):
            return JSONResponse({"detail": f"host not trusted: {host or '(missing)'}"}, status_code=400)
        return await call_next(req)


class RateLimitMiddleware(Middleware):
    """Fixed-window in-memory rate limiter (per-IP). Swap with Redis for multi-node.

    429s carry Retry-After + X-RateLimit-* headers per RFC 6585 / IETF draft.
    Pass clock= for deterministic tests.
    """

    def __init__(self, per_minute: int = 120, clock: Any = None, max_buckets: int = 50_000):
        self.limit = per_minute
        self._clock = clock or time.time
        self._hits: dict[str, tuple[int, float]] = {}
        # Buckets are per-IP: cap the table so a distributed scan can't
        # grow memory without bound; oldest windows are evicted first.
        self.max_buckets = max_buckets

    def _evict(self, now: float) -> None:
        # expired windows first; then oldest starts (least likely active)
        for ip in [ip for ip, (_, start) in self._hits.items() if now - start >= 60]:
            self._hits.pop(ip, None)
        while len(self._hits) > self.max_buckets:
            oldest = min(self._hits, key=lambda ip: self._hits[ip][1])
            self._hits.pop(oldest, None)

    def _headers(self, count: int, reset_in: float) -> dict[str, str]:
        return {
            "x-ratelimit-limit": str(self.limit),
            "x-ratelimit-remaining": str(max(0, self.limit - count)),
            "x-ratelimit-reset": str(max(0, int(reset_in))),
        }

    async def __call__(self, req: Any, call_next: Any) -> Any:
        fwd = req.headers.get("x-forwarded-for", "")
        if fwd:
            ip = fwd.split(",")[0].strip()
        else:
            # Direct connection (no proxy): scope client, not one shared
            # bucket — otherwise every visitor rate-limits everyone.
            client = getattr(req, "scope", {}).get("client", None)
            ip = client[0] if client else "local"
        now = self._clock()
        count, start = self._hits.get(ip, (0, now))
        if now - start >= 60:
            count, start = 0, now
        count += 1
        self._hits[ip] = (count, start)
        if len(self._hits) > self.max_buckets:
            self._evict(now)
        reset_in = 60 - (now - start)
        if count > self.limit:
            resp = JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
            resp.headers.update(self._headers(count, reset_in))
            resp.headers["retry-after"] = str(max(1, int(reset_in)))
            return resp
        resp = await call_next(req)
        resp.headers.update(self._headers(count, reset_in))
        return resp


class RedisRateLimitMiddleware(Middleware):
    """Fixed-window rate limiting over a shared cache (multi-process).

    Same 429 + ``Retry-After`` + ``X-RateLimit-*`` contract as
    :class:`RateLimitMiddleware`, keyed per IP per 60s window with a 65s
    TTL — every worker counts against one table instead of its own::

        app.use(RedisRateLimitMiddleware(RedisCache(url=...)))

    Approximation, stated plainly: increments are read-modify-write, so
    a thundering herd can slip a few requests past the line. Exact
    enough for abuse protection; not a billing meter. ``clock=`` keeps
    tests deterministic.
    """

    def __init__(self, cache: Any, per_minute: int = 120, clock: Any = None):
        if cache is None:
            raise ValueError("RedisRateLimitMiddleware needs a cache: pass RedisCache(url=...)")
        self.cache = cache
        self.limit = per_minute
        self._clock = clock or time.time

    def _key(self, ip: str, window: int) -> str:
        return f"ratelimit:{ip}:{window}"

    def _headers(self, count: int, reset_in: float) -> dict[str, str]:
        return {
            "x-ratelimit-limit": str(self.limit),
            "x-ratelimit-remaining": str(max(0, self.limit - count)),
            "x-ratelimit-reset": str(max(0, int(reset_in))),
        }

    async def __call__(self, req: Any, call_next: Any) -> Any:
        fwd = req.headers.get("x-forwarded-for", "")
        if fwd:
            ip = fwd.split(",")[0].strip()
        else:
            client = getattr(req, "scope", {}).get("client", None)
            ip = client[0] if client else "local"
        now = self._clock()
        window = int(now // 60)
        key = self._key(ip, window)
        state = await self.cache.get(key) or {}
        count = int(state.get("n", 0)) + 1
        await self.cache.set(key, {"n": count, "start": window * 60}, ttl=65)
        reset_in = 60 - (now - window * 60)
        if count > self.limit:
            resp = JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
            resp.headers.update(self._headers(count, reset_in))
            resp.headers["retry-after"] = str(max(1, int(reset_in)))
            return resp
        resp = await call_next(req)
        resp.headers.update(self._headers(count, reset_in))
        return resp
