"""Trust batch: fail fast, fail loud, velocity, power — the emotional checklist."""

import asyncio

import pytest

from ikarem import (
    CSRFMiddleware,
    Depends,
    Field,
    Ikarem,
    Schema,
    SessionMiddleware,
    deprecated,
    require_roles,
)
from ikarem.deprecation import deprecated as deprecated2
from ikarem.security import RateLimitMiddleware
from ikarem.session import encode_session
from ikarem.testing import TestClient


class _FutureItem(Schema):
    name: str


def test_string_annotations_resolve_like_future_import():
    """Handlers written under `from __future__ import annotations` store
    strings; plans must resolve them via the handler's globals."""

    async def h(req, item):
        return {"name": item.name}

    h.__annotations__ = {"item": "_FutureItem"}
    app = Ikarem(enable_docs=False)
    app.router.add("/x", {"POST"}, h)
    assert TestClient(app).post("/x", body={"name": "amy"}).json() == {"name": "amy"}


def test_optional_schema_default_still_validates_on_all_pythons():
    """`body: Item = None` must validate on every version. Python ≤3.10's
    get_type_hints wraps it as Optional[Item] (3.11+ does not) — without
    unwrapping, 3.10 silently skipped validation (200 on garbage)."""
    from typing import Optional

    from ikarem.compiled import get_plan

    class _Box(Schema):
        amount: float

    async def h(req, body: Optional[_Box] = None):
        return {"ok": True}

    kinds = {p.name: p.kind for p in get_plan(h).params}
    assert kinds["body"] == "schema", kinds
    app = Ikarem(enable_docs=False)
    app.router.add("/box", {"POST"}, h)
    c = TestClient(app)
    assert c.post("/box", body={"amount": 1}).status_code == 200
    assert c.post("/box", body={"nope": 1}).status_code == 400


def test_startup_refuses_default_secret_with_auth_routes():
    app = Ikarem(enable_docs=False)

    @app.get("/admin")
    async def adm(req, claims=Depends(require_roles("admin"))):
        return {"ok": True}

    with pytest.raises(RuntimeError, match="auth_secret"):
        TestClient(app).get("/admin")


def test_startup_refuses_missing_session_secret():
    app = Ikarem(enable_docs=False)
    app.use(SessionMiddleware())

    @app.get("/")
    async def h(req):
        return {"ok": True}

    with pytest.raises(RuntimeError, match="secret"):
        TestClient(app).get("/")


def test_startup_refuses_csrf_without_session():
    app = Ikarem(enable_docs=False, session_secret="s")
    app.use(CSRFMiddleware())  # no SessionMiddleware before it

    @app.get("/")
    async def h(req):
        return {"ok": True}

    with pytest.raises(RuntimeError, match="SessionMiddleware.*before"):
        TestClient(app).get("/")


def test_bearer_with_own_secret_needs_no_config():
    from ikarem import BearerAuth, create_token

    app = Ikarem(enable_docs=False)
    gate = BearerAuth("own-secret")

    @app.get("/me")
    async def me(req, claims=Depends(gate)):
        return {"sub": claims["sub"]}

    tok = create_token("u9", "own-secret")
    assert TestClient(app).get("/me", headers={"authorization": f"Bearer {tok}"}).status_code == 200


def test_check_flags_duplicate_routes():
    app = Ikarem(enable_docs=False)

    @app.get("/dup")
    async def one(req):
        return {"n": 1}

    @app.get("/dup")
    async def two(req):
        return {"n": 2}

    rep = app.check()
    assert any("duplicate route" in e and "one" in e and "two" in e for e in rep["errors"])


def test_404_suggests_close_match():
    app = Ikarem(enable_docs=False)

    @app.get("/users")
    async def u(req):
        return []

    r = TestClient(app).get("/usres")
    assert r.status_code == 404
    assert "/users" in r.json()["detail"]


def test_rate_limit_uses_scope_client_ip():
    now = [1000.0]
    mw = RateLimitMiddleware(per_minute=1, clock=lambda: now[0])
    app = Ikarem(enable_docs=False)
    app.use(mw)

    @app.get("/r")
    async def r(req):
        return "ok"

    # same client, different scope IPs (no proxy header): separate buckets
    async def get_with_ip(ip):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/r",
            "query_string": b"",
            "headers": [],
            "server": ("t", 80),
            "client": (ip, 5000),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        msgs = []

        async def send(m):
            msgs.append(m)

        await app.startup()
        await app(scope, receive, send)
        return next(m["status"] for m in msgs if m["type"] == "http.response.start")

    async def go():
        assert await get_with_ip("1.1.1.1") == 200
        assert await get_with_ip("1.1.1.1") == 429  # same IP: limited
        assert await get_with_ip("2.2.2.2") == 200  # different IP: fresh bucket

    asyncio.run(go())
    assert len(mw._hits) <= 2


def _boot_resource_app():
    from ikarem.db import DatabasePlugin

    app = Ikarem(enable_docs=False, session_secret="s")
    app.use(SessionMiddleware())
    app.register(DatabasePlugin("sqlite:///:memory:"))

    class Note(Schema):
        text: str = Field(..., min_length=1)
        user_id: str = "ATTACKER"

    async def boot():
        await app.startup()
        await app.state_db.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, text TEXT)"
        )

    asyncio.run(boot())
    app.resource("/notes", Note, table="notes", owner_field="user_id")
    return app


def _authed(app, uid):
    c = TestClient(app)
    c.cookies["ikarem_session"] = encode_session({"uid": uid}, "s", 3600)
    return c


def test_resource_owner_spoof_and_crud():
    app = _boot_resource_app()
    assert TestClient(app).get("/notes").status_code == 401  # anon: 401, never a leak
    assert TestClient(app).post("/notes", body={}).status_code in (400, 401)
    a, b = _authed(app, "u-a"), _authed(app, "u-b")
    assert a.get("/notes").json() == {"total": 0, "page": 1, "items": []}
    r = a.post("/notes", body={"text": "hello", "user_id": "u-b"})
    assert r.status_code == 201
    assert r.json()["user_id"] == "u-a"  # spoof ignored: session wins
    nid = r.json()["id"]
    assert b.get(f"/notes/{nid}").status_code == 404  # foreign row invisible
    assert b.put(f"/notes/{nid}", body={"text": "hijack", "user_id": "u-b"}).status_code == 404
    assert b.delete(f"/notes/{nid}").status_code == 404
    assert a.get(f"/notes/{nid}").json()["text"] == "hello"
    assert a.put(f"/notes/{nid}", body={"text": "edited"}).json()["text"] == "edited"
    assert a.delete(f"/notes/{nid}").json() == {"ok": True}
    assert a.get(f"/notes/{nid}").status_code == 404


def test_mount_asgi_delegates_untouched():
    async def subapp(scope, receive, send):
        body = b"raw:" + scope.get("path", "/").encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    app = Ikarem(enable_docs=False)
    app.mount_asgi("/sub", subapp)

    @app.get("/")
    async def home(req):
        return {"framework": "ikarem"}

    c = TestClient(app)
    assert c.get("/sub/deep/path").text == "raw:/sub/deep/path"
    assert c.get("/sub").text == "raw:/sub"
    assert c.get("/").json() == {"framework": "ikarem"}


def test_deprecated_warns_with_path():
    from ikarem import deprecated as d2

    assert deprecated is deprecated2

    @d2("it talks to the old API", since="1.1.0", removal="2.0.0", use_instead="new_fetch")
    def old_fetch():
        return 42

    with pytest.warns(DeprecationWarning, match="removed in 2.0.0.*Use new_fetch instead"):
        assert old_fetch() == 42
