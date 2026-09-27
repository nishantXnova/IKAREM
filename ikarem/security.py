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
    async def __call__(self, req: Any, call_next: Any) -> Any:
        resp = await call_next(req)
        resp.headers.setdefault("x-content-type-options", "nosniff")
        resp.headers.setdefault("x-frame-options", "DENY")
        resp.headers.setdefault("referrer-policy", "no-referrer")
        return resp


class RateLimitMiddleware(Middleware):
    """Fixed-window in-memory rate limiter (per-IP). Swap with Redis for multi-node.

    429s carry Retry-After + X-RateLimit-* headers per RFC 6585 / IETF draft.
    Pass clock= for deterministic tests.
    """

    def __init__(self, per_minute: int = 120, clock: Any = None):
        self.limit = per_minute
        self._clock = clock or time.time
        self._hits: dict[str, tuple[int, float]] = {}

    def _headers(self, count: int, reset_in: float) -> dict[str, str]:
        return {
            "x-ratelimit-limit": str(self.limit),
            "x-ratelimit-remaining": str(max(0, self.limit - count)),
            "x-ratelimit-reset": str(max(0, int(reset_in))),
        }

    async def __call__(self, req: Any, call_next: Any) -> Any:
        ip = req.headers.get("x-forwarded-for", "local").split(",")[0].strip()
        now = self._clock()
        count, start = self._hits.get(ip, (0, now))
        if now - start >= 60:
            count, start = 0, now
        count += 1
        self._hits[ip] = (count, start)
        reset_in = 60 - (now - start)
        if count > self.limit:
            resp = JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
            resp.headers.update(self._headers(count, reset_in))
            resp.headers["retry-after"] = str(max(1, int(reset_in)))
            return resp
        resp = await call_next(req)
        resp.headers.update(self._headers(count, reset_in))
        return resp
