"""Observability: JSON logging, request-id + timing middleware, health/metrics."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from .middleware import Middleware

logger = logging.getLogger("ikarem")


def configure_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter('{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'))
    logger.handlers = [h]
    logger.setLevel(level)


class RequestIDMiddleware(Middleware):
    async def __call__(self, req: Any, call_next: Any) -> Any:
        rid = req.headers.get("x-request-id", uuid.uuid4().hex[:12])
        req.state["request_id"] = rid
        start = time.perf_counter()
        resp = await call_next(req)
        resp.headers["x-request-id"] = rid
        resp.headers["x-process-time-ms"] = f"{(time.perf_counter() - start) * 1000:.2f}"
        logger.info(f"{req.method} {req.path} -> {resp.status_code} rid={rid}")
        return resp


_METRICS: dict[str, Any] = {"requests": 0, "errors": 0, "start": time.time()}


class MetricsMiddleware(Middleware):
    async def __call__(self, req: Any, call_next: Any) -> Any:
        _METRICS["requests"] += 1
        try:
            return await call_next(req)
        except Exception:
            _METRICS["errors"] += 1
            raise


def mount_system_routes(app: Any) -> None:
    from .http import JSONResponse, TextResponse

    async def _health(req: Any) -> Any:
        return {"status": "ok", "framework": "ikarem"}

    async def _ready(req: Any) -> Any:
        """Readiness: 200 only when optional dependencies are actually up.

        Today that means the database (when DatabasePlugin is registered):
        a failed ping returns 503 so load balancers stop sending traffic.
        """
        db = getattr(req.app, "state_db", None)
        if db is None:
            return {"status": "ready", "db": "none"}
        try:
            await db.fetch_one("SELECT 1")
            return {"status": "ready", "db": "ok"}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"status": "not-ready", "db": str(e)[:200]}, status_code=503)

    async def _metrics(req: Any) -> Any:
        uptime = time.time() - _METRICS["start"]
        body = f"# ikarem metrics\nikarem_requests {_METRICS['requests']}\nikarem_errors {_METRICS['errors']}\nikarem_uptime_seconds {uptime:.1f}\n"
        return TextResponse(body)

    _health._ikarem_internal = True  # type: ignore
    _metrics._ikarem_internal = True  # type: ignore
    _ready._ikarem_internal = True  # type: ignore
    # No silent try/except: _ensure_system_routes guards double-mount, and a
    # real registration failure must surface, not vanish.
    app.router.add("/healthz", {"GET"}, _health, name="healthz")
    app.router.add("/readyz", {"GET"}, _ready, name="readyz")
    app.router.add("/metrics", {"GET"}, _metrics, name="metrics")
