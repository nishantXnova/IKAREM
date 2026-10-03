"""Industry-grade proof: tracing without the SDK, limits across processes."""

import asyncio
import sys

import pytest

from ikarem import Ikarem, RedisRateLimitMiddleware, TracingMiddleware
from ikarem.cache import RedisCache
from ikarem.testing import TestClient


class _Span:
    def __init__(self, name, log):
        self.name = name
        self.attrs = {}
        self.events = []
        self._log = log

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def record_exception(self, e):
        self.events.append(repr(e))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._log.append(self)
        return False


class _FakeTracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name, **kwargs):
        return _Span(name, self.spans)


def _traced_app(tracer):
    app = Ikarem(enable_docs=False)
    app.use(TracingMiddleware(tracer=tracer))

    @app.get("/ok")
    async def ok(req):
        return {"ok": True}

    @app.get("/bad")
    async def bad(req):
        raise ValueError("nope")

    return app


def test_tracing_records_attributes_and_status():
    tracer = _FakeTracer()
    c = TestClient(_traced_app(tracer))
    assert c.get("/ok").status_code == 200
    (span,) = tracer.spans
    assert span.name == "GET /ok"
    assert span.attrs["http.method"] == "GET"
    assert span.attrs["http.target"] == "/ok"
    assert span.attrs["http.status_code"] == 200
    assert span.events == []


def test_tracing_marks_500s_and_records_escapes():
    # Handler errors render inside the pipeline (status 500 on the span);
    # escapes past the pipeline record the exception and still propagate.
    tracer = _FakeTracer()
    c = TestClient(_traced_app(tracer))
    assert c.get("/bad").status_code == 500
    assert tracer.spans[0].attrs["http.status_code"] == 500

    async def _boom(req):
        raise RuntimeError("downstream blew up")

    async def _go():
        from ikarem.http import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "server": ("t", 80),
            "client": ("t", 1),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        req = Request(scope, receive)
        with pytest.raises(RuntimeError, match="blew up"):
            await TracingMiddleware(tracer=tracer)(req, _boom)

    asyncio.run(_go())
    assert any("RuntimeError" in e for e in tracer.spans[-1].events)


def test_tracing_without_sdk_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
    with pytest.raises(RuntimeError, match=r"pip install ikarem\[otel\]"):
        TracingMiddleware()._resolve_tracer()


class _FakeRedis:
    """Raw string storage like a real server; RedisCache serializes above it."""

    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def setex(self, k, ttl, v):
        self.d[k] = v.decode() if isinstance(v, bytes) else v

    async def delete(self, k):
        self.d.pop(k, None)


def _limited_app(cache, clock, per_minute=2):
    app = Ikarem(enable_docs=False)
    app.use(RedisRateLimitMiddleware(cache, per_minute=per_minute, clock=clock))

    @app.get("/")
    async def home(req):
        return {"ok": True}

    return app


def test_redis_limiter_shared_across_processes():
    now = [1000.0]
    cache = RedisCache(prefix="rl:", client=_FakeRedis())
    c1 = TestClient(_limited_app(cache, lambda: now[0]))
    c2 = TestClient(_limited_app(cache, lambda: now[0]))  # second "process", same table
    assert c1.get("/").status_code == 200
    assert c2.get("/").status_code == 200
    r = c1.get("/")
    assert r.status_code == 429
    assert r.headers["retry-after"] and r.headers["x-ratelimit-limit"] == "2"
    now[0] += 61.0  # next window: budget restored everywhere
    assert c2.get("/").status_code == 200


def test_redis_limiter_needs_cache():
    with pytest.raises(ValueError, match="RedisCache"):
        RedisRateLimitMiddleware(None)
