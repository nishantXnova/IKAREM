"""Reference adapter proof: outside middleware runs the native pipeline."""

import pytest

from adapters.asgi_bridge import ASGIMiddlewareAdapter
from ikarem import Ikarem
from ikarem.testing import TestClient


def _add_header_factory(app):
    async def wrapped(scope, receive, send):
        async def send_with_header(msg):
            if msg["type"] == "http.response.start":
                msg = {**msg, "headers": [*msg.get("headers", []), (b"x-donor", b"yes")]}
            await send(msg)

        await app(scope, receive, send_with_header)

    return wrapped


def _block_factory(app):
    async def wrapped(scope, receive, send):
        if scope.get("path") == "/blocked":
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"blocked-by-donor"})
            return
        await app(scope, receive, send)

    return wrapped


def test_donor_header_passes_through_and_handler_runs():
    app = Ikarem(enable_docs=False)
    app.use(ASGIMiddlewareAdapter(_add_header_factory))

    @app.get("/")
    async def h(req):
        return {"ok": True}

    r = TestClient(app).get("/")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers.get("x-donor") == "yes"


def test_donor_short_circuit_never_reaches_handler():
    app = Ikarem(enable_docs=False)
    app.use(ASGIMiddlewareAdapter(_block_factory))
    calls = []

    @app.get("/blocked")
    async def h(req):
        calls.append(1)
        return {"never": True}

    @app.get("/open")
    async def o(req):
        return {"ok": True}

    c = TestClient(app)
    r = c.get("/blocked")
    assert r.status_code == 403 and r.text == "blocked-by-donor" and calls == []
    assert c.get("/open").json() == {"ok": True}


def test_donor_scope_mutation_does_not_leak_into_routing():
    def rewrite_factory(app):
        async def wrapped(scope, receive, send):
            scope["path"] = "/rewritten"  # hits the copy only

            await app(scope, receive, send)

        return wrapped

    app = Ikarem(enable_docs=False)
    app.use(ASGIMiddlewareAdapter(rewrite_factory))

    @app.get("/real")
    async def h(req):
        return {"path": req.path}

    r = TestClient(app).get("/real")
    assert r.status_code == 200 and r.json() == {"path": "/real"}


def test_donor_reading_body_first_keeps_handler_body_intact():
    seen = {}

    def reader_factory(app):
        async def wrapped(scope, receive, send):
            total = 0
            while True:
                msg = await receive()
                if msg.get("type") != "http.request":
                    break
                total += len(msg.get("body", b""))
                if not msg.get("more_body"):
                    break
            seen["n"] = total
            await app(scope, receive, send)

        return wrapped

    app = Ikarem(enable_docs=False)
    app.use(ASGIMiddlewareAdapter(reader_factory))

    @app.post("/echo")
    async def echo(req):
        return await req.json()

    import json as _json

    r = TestClient(app).post("/echo", body={"hello": "world"})
    assert r.status_code == 200 and r.json() == {"hello": "world"}
    assert seen["n"] == len(_json.dumps({"hello": "world"}).encode())


def test_donor_chunked_response_buffered_into_one_body():
    def chunked_factory(app):
        async def wrapped(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            for part in (b"a", b"b", b"c"):
                await send({"type": "http.response.body", "body": part, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        return wrapped

    app = Ikarem(enable_docs=False)
    app.use(ASGIMiddlewareAdapter(chunked_factory))

    @app.get("/")
    async def h(req):  # pragma: no cover - donor never calls downstream
        return {"never": True}

    r = TestClient(app).get("/")
    assert r.status_code == 200 and r.text == "abc"


def test_donor_repeated_headers_survive_as_multi():
    def multi_factory(app):
        async def wrapped(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"x-multi", b"1"),
                        (b"x-multi", b"2"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        return wrapped

    app = Ikarem(enable_docs=False)
    app.use(ASGIMiddlewareAdapter(multi_factory))

    @app.get("/")
    async def h(req):  # pragma: no cover - donor never calls downstream
        return {"never": True}

    r = TestClient(app).get("/")
    vals = [v for k, v in r.headers_list if k == "x-multi"]
    assert vals == ["1", "2"]


def test_adapted_traffic_runs_full_native_pipeline():
    app = Ikarem(enable_docs=False)
    app.use(ASGIMiddlewareAdapter(_add_header_factory))
    # No route registered: the donor passes through, the native router 404s —
    # adapted traffic is audited, routed, and rendered by the core, not around it.
    r = TestClient(app).get("/missing")
    assert r.status_code == 404 and "No route for GET /missing" in r.json()["detail"]
    assert r.headers.get("x-donor") == "yes"  # donor after-hooks still applied


def test_bad_factory_rejected_with_fix():
    with pytest.raises(ValueError, match="factory taking the downstream"):
        ASGIMiddlewareAdapter("not-a-factory")  # type: ignore
    with pytest.raises(ValueError, match="must return an ASGI app"):
        ASGIMiddlewareAdapter(lambda app: 42)
    with pytest.raises(ValueError, match="failed at registration"):
        ASGIMiddlewareAdapter(lambda app: 1 / 0)
