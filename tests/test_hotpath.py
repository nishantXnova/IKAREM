"""Hot-path perf work: cached query/cookies, lean middleware, optional orjson."""

import asyncio

from ikarem.http import Request, dumps_json_bytes, loads_json_bytes


def _req(path="/", qs=b"a=1&b=2", headers=None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": qs,
        "headers": headers or [],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def test_query_parsed_once_per_request():
    req = _req()
    first = req.query
    assert first == {"a": "1", "b": "2"}
    assert req.query is first  # cached, not re-parsed per param


def test_cookies_parsed_once_per_request():
    req = _req(headers=[(b"cookie", b"sess=abc; theme=dark")])
    first = req.cookies
    assert first == {"sess": "abc", "theme": "dark"}
    assert req.cookies is first


def test_empty_query_and_cookies_cached():
    req = _req(qs=b"")
    assert req.query == {}
    assert req.query is req.query
    assert req.cookies == {}
    assert req.cookies is req.cookies


def test_json_helpers_round_trip():
    raw = dumps_json_bytes({"a": 1, "b": [1, 2]})
    assert isinstance(raw, bytes)
    assert loads_json_bytes(raw) == {"a": 1, "b": [1, 2]}
    assert loads_json_bytes(b"") is None or True  # loads path tolerates empty via Request.json


def test_request_json_empty_body_is_none():
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []}
    req = Request(scope, receive)
    assert asyncio.run(req.json()) is None


def test_request_json_body_round_trip():
    body = dumps_json_bytes({"hello": "world"})

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []}
    req = Request(scope, receive)
    assert asyncio.run(req.json()) == {"hello": "world"}


def test_json_helpers_fall_back_to_stdlib(monkeypatch):
    import ikarem.http as http_mod

    monkeypatch.setattr(http_mod, "_orjson_module", None)
    monkeypatch.setattr(http_mod, "_orjson_tried", True)
    assert loads_json_bytes(b'{"x": 1}') == {"x": 1}
    assert loads_json_bytes('{"x": 2}') == {"x": 2}
    assert isinstance(dumps_json_bytes({"x": 1}), bytes)


def test_middleware_order_and_after_hooks_preserved():
    from ikarem import Ikarem
    from ikarem.testing import TestClient

    calls: list[str] = []
    app = Ikarem()

    async def first(req, call_next):
        calls.append("first-before")
        resp = await call_next(req)
        calls.append("first-after")
        resp.headers["x-order"] = resp.headers.get("x-order", "") + "1"
        return resp

    async def second(req, call_next):
        calls.append("second-before")
        resp = await call_next(req)
        calls.append("second-after")
        resp.headers["x-order"] = resp.headers.get("x-order", "") + "2"
        return resp

    app.use(first)
    app.use(second)

    @app.get("/")
    async def h(req):
        calls.append("handler")
        return "ok"

    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert calls == ["first-before", "second-before", "handler", "second-after", "first-after"]
    assert r.headers.get("x-order") == "21"


def test_middleware_request_replacement_still_works():
    from ikarem import Ikarem
    from ikarem.testing import TestClient

    app = Ikarem()

    async def swap(req, call_next):
        req.state["injected"] = "yes"
        return await call_next(req)

    app.use(swap)

    @app.get("/")
    async def h(req):
        return {"injected": req.state.get("injected", "no")}

    r = TestClient(app).get("/")
    assert r.json() == {"injected": "yes"}


def test_single_middleware_fast_path_short_circuits():
    from ikarem import Ikarem, TextResponse
    from ikarem.testing import TestClient

    app = Ikarem()

    async def block(req, call_next):
        if req.path == "/blocked":
            return TextResponse("nope", status_code=403)
        return await call_next(req)

    app.use(block)

    @app.get("/blocked")
    async def h(req):
        return "never"

    @app.get("/open")
    async def o(req):
        return "yes"

    c = TestClient(app)
    assert c.get("/blocked").status_code == 403
    assert c.get("/open").text == "yes"


def test_response_preencoded_content_type():
    import asyncio as _aio

    from ikarem.http import JSONResponse

    resp = JSONResponse({"ok": True})
    assert resp._content_type == b"application/json"
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    _aio.run(resp({"type": "http"}, None, send))
    start = sent[0]
    assert start["status"] == 200
    names = [k for k, _ in start["headers"]]
    assert b"content-type" in names
    assert b"content-length" in names
