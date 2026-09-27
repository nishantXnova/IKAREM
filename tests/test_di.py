"""Exhaustive DI verification: Depends() x12 branches."""

import asyncio

import pytest

from ikarem import Depends, Ikarem
from ikarem.di import resolve_handler, run_cleanups
from ikarem.errors import Unauthorized
from ikarem.http import Request
from ikarem.testing import TestClient


def _req(path="/", query=""):
    async def _recv():
        return {"type": "http.disconnect"}

    return Request(
        {"type": "http", "method": "GET", "path": path, "query_string": query.encode(), "headers": []},
        _recv,
    )


def test_01_primitive_dependency():
    app = Ikarem(enable_docs=False)

    @app.get("/p")
    async def h(req, val=Depends(42)):
        return {"val": val}

    assert TestClient(app).get("/p").json() == {"val": 42}


def test_02_nested_dependency():
    app = Ikarem(enable_docs=False)

    def base():
        return 10

    def mid(v=Depends(base)):
        return v * 2

    @app.get("/n")
    async def h(req, v=Depends(mid)):
        return {"v": v}

    assert TestClient(app).get("/n").json() == {"v": 20}


def test_03_dependency_parameters():
    """Deps read their own defaults, path params, and query params."""
    app = Ikarem(enable_docs=False)

    def pager(limit: int = 5):
        return limit

    @app.get("/items")
    async def h(req, limit=Depends(pager)):
        return {"limit": limit}

    c = TestClient(app)
    assert c.get("/items").json() == {"limit": 5}
    assert c.get("/items", query="limit=7").json() == {"limit": 7}


def test_04_per_request_caching():
    calls = []

    def dep():
        calls.append(1)
        return len(calls)

    async def h(req, a=Depends(dep), b=Depends(dep)):
        return {"a": a, "b": b}

    out = asyncio.run(resolve_handler(h, _req()))
    assert out == {"a": 1, "b": 1} and len(calls) == 1


def test_05_cache_disabled():
    calls = []

    def dep():
        calls.append(1)
        return len(calls)

    async def h(req, a=Depends(dep, use_cache=False), b=Depends(dep, use_cache=False)):
        return {"a": a, "b": b}

    out = asyncio.run(resolve_handler(h, _req()))
    assert (out["a"], out["b"]) == (1, 2) and len(calls) == 2


def test_06_async_dependency():
    async def dep():
        return "async-ok"

    async def h(req, v=Depends(dep)):
        return {"v": v}

    assert asyncio.run(resolve_handler(h, _req())) == {"v": "async-ok"}


def test_07_sync_dependency():
    def dep():
        return "sync-ok"

    async def h(req, v=Depends(dep)):
        return {"v": v}

    assert asyncio.run(resolve_handler(h, _req())) == {"v": "sync-ok"}


def test_08_yield_dependency_value_and_cleanup_registered():
    def dep():
        yield "yielded"

    async def h(req, v=Depends(dep)):
        return {"v": v}

    req = _req()
    out = asyncio.run(resolve_handler(h, req))
    assert out == {"v": "yielded"}
    asyncio.run(run_cleanups(req))  # standalone callers finalize explicitly


def test_09_cleanup_after_response():
    """Cleanup MUST run after the response bytes are sent (proved via send order)."""
    from ikarem import TextResponse

    events = []

    class MarkResponse(TextResponse):
        async def __call__(self, scope, receive, send):
            await super().__call__(scope, receive, send)
            events.append("sent")

    def dep():
        events.append("enter")
        yield "d"
        events.append("cleanup")

    app = Ikarem(enable_docs=False)

    @app.get("/r")
    async def h(req, d=Depends(dep)):
        events.append("handler")
        return MarkResponse("ok")

    assert TestClient(app).get("/r").status_code == 200
    assert events == ["enter", "handler", "sent", "cleanup"], events


def test_10_cleanup_after_exception():
    events = []

    def dep():
        try:
            yield "d"
        finally:
            events.append("cleanup")

    app = Ikarem(enable_docs=False)

    @app.get("/boom")
    async def h(req, d=Depends(dep)):
        raise RuntimeError("handler failed")

    assert TestClient(app).get("/boom").status_code == 500
    assert events == ["cleanup"], events


def test_11_dependency_failure_maps_to_status():
    def guard():
        raise Unauthorized("no entry")

    app = Ikarem(enable_docs=False)

    @app.get("/g")
    async def h(req, g=Depends(guard)):
        return {"never": True}

    r = TestClient(app).get("/g")
    assert r.status_code == 401
    assert r.json() == {"detail": "no entry"}


def test_12_circular_dependency_rejected():
    def dep_a(b=Depends(lambda: None)):
        return b

    def dep_b(a=Depends(dep_a)):
        return a

    dep_a.__defaults__ = (Depends(dep_b),)  # a -> b -> a

    async def h(req, v=Depends(dep_a)):
        return {"v": v}

    with pytest.raises(ValueError, match="[Cc]ircular"):
        asyncio.run(resolve_handler(h, _req()))
