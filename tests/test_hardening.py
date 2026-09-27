"""Hardening: bounded tables, traversal, body bombs, encoding, websockets, fuzz."""

import asyncio
import os
import random

import pytest

from ikarem import Ikarem
from ikarem.cache import MemoryCache
from ikarem.security import RateLimitMiddleware
from ikarem.testing import TestClient


def test_cache_bounded_and_sweeps_expired():
    async def go():
        c = MemoryCache(maxsize=10)
        for i in range(50):
            await c.set(f"k{i}", i, ttl=60)
        assert len(c._d) <= 10
        await c.set("old", 1, ttl=-1)
        await c.set("fresh", 2, ttl=60)
        assert await c.get("old") is None
        assert await c.get("fresh") == 2

    asyncio.run(go())


def test_ratelimit_table_bounded():
    now = [1000.0]
    mw = RateLimitMiddleware(per_minute=1000, clock=lambda: now[0], max_buckets=20)
    app = Ikarem(enable_docs=False)
    app.use(mw)

    @app.get("/r")
    async def r(req):
        return "ok"

    c = TestClient(app)
    for i in range(200):
        c.get("/r", headers={"x-forwarded-for": f"10.0.0.{i}"})
    assert len(mw._hits) <= 20


def test_static_blocks_dotdot_and_prefix_collision(tmp_path):
    pub = tmp_path / "pub"
    pub.mkdir()
    (pub / "ok.txt").write_text("hi")
    (tmp_path / "secret.txt").write_text("nope")
    other = tmp_path / "public-evil"
    other.mkdir()
    (other / "x.txt").write_text("nope")

    app = Ikarem(enable_docs=False)
    app.mount_static("/static", str(pub))
    c = TestClient(app)
    assert c.get("/static/ok.txt").status_code == 200
    assert c.get("/static/../secret.txt").status_code == 404
    # /public-evil shares a string prefix with /pub — must not escape
    app2 = Ikarem(enable_docs=False)
    app2.mount_static("/s", str(pub))
    assert TestClient(app2).get("/s/../public-evil/x.txt").status_code == 404


def test_static_blocks_symlink_escape(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    pub = tmp_path / "pub"
    pub.mkdir()
    try:
        os.symlink(outside, pub / "link.txt")
    except OSError:
        pytest.skip("symlinks need privileges on this machine")
    app = Ikarem(enable_docs=False)
    app.mount_static("/static", str(pub))
    assert TestClient(app).get("/static/link.txt").status_code == 404


def test_body_cap_enforced_while_streaming():
    app = Ikarem(enable_docs=False)

    @app.post("/b")
    async def b(req):
        await req.body(max_bytes=10)
        return {"ok": True}

    async def go():
        from ikarem.http import Request

        chunks = [b"x" * 8, b"y" * 8]

        async def receive():
            if chunks:
                return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
            return {"type": "http.disconnect"}

        from ikarem.errors import PayloadTooLarge

        req = Request(
            {"type": "http", "method": "POST", "path": "/b", "query_string": b"", "headers": []}, receive
        )
        with pytest.raises(PayloadTooLarge):
            await req.body(max_bytes=10)

    asyncio.run(go())
    c = TestClient(app)
    assert c.post("/b", body="x" * 100).status_code == 413


def test_multipart_undecodable_content_type_is_413():
    app = Ikarem(enable_docs=False)

    @app.post("/up")
    async def up(req):
        await req.form()
        return {"ok": True}

    c = TestClient(app)
    # boundary outside latin-1: old code 500'd in .encode("latin-1")
    r = c.post("/up", body=b"--x\r\n", content_type="multipart/form-data; boundary=\u4e2d\u6587")
    assert r.status_code == 413


def test_readyz_no_db_db_ok_db_down():
    assert TestClient(Ikarem(enable_docs=False)).get("/readyz").json() == {"status": "ready", "db": "none"}

    from ikarem.db import DatabasePlugin

    app = Ikarem(enable_docs=False)
    app.register(DatabasePlugin("sqlite:///:memory:"))
    c = TestClient(app)
    assert c.get("/readyz").json() == {"status": "ready", "db": "ok"}

    async def kill():
        await app.state_db.disconnect()

    asyncio.run(kill())
    # fresh client would restart via startup(); reuse same app without startup
    r = asyncio.run(c._do("GET", "/readyz", None, {}, ""))
    assert r.status_code == 503
    assert r.json()["status"] == "not-ready"


def _ws_call(app, path, incoming):
    msgs_in = list(incoming)
    sent = []

    async def receive():
        if msgs_in:
            return msgs_in.pop(0)
        await asyncio.sleep(3600)
        return {"type": "websocket.disconnect"}

    async def send(m):
        sent.append(m)

    scope = {
        "type": "websocket",
        "path": path,
        "headers": [],
        "query_string": b"",
        "server": ("t", 80),
        "client": ("t", 1),
    }
    asyncio.run(app(scope, receive, send))
    return sent


def test_websocket_echo():
    app = Ikarem(enable_docs=False)

    @app.websocket("/chat")
    async def chat(ws):
        await ws.accept()
        await ws.send_text("echo:" + await ws.receive_text())

    sent = _ws_call(app, "/chat", [{"type": "websocket.connect"}, {"text": "hi"}])
    assert {"type": "websocket.accept"} in sent
    assert {"type": "websocket.send", "text": "echo:hi"} in sent


def test_websocket_unknown_path_and_handler_crash():
    app = Ikarem(enable_docs=False)

    @app.websocket("/ok")
    async def ok(ws):
        await ws.accept()

    sent = _ws_call(app, "/nope", [])
    assert sent == [{"type": "websocket.close", "code": 4404}]

    app2 = Ikarem(enable_docs=False)

    @app2.websocket("/boom")
    async def boom(ws):
        await ws.accept()
        raise RuntimeError("x")

    sent = _ws_call(app2, "/boom", [{"type": "websocket.connect"}])
    assert {"type": "websocket.close", "code": 1011} in sent


def test_fuzz_routing_converters():
    from ikarem import Ikarem as A

    app = A(enable_docs=False)

    @app.get("/u/{uid:int}")
    async def u(req, uid: int):
        return {"uid": uid, "t": type(uid).__name__}

    @app.get("/f/{p:path}")
    async def f(req, p: str):
        return {"p": p}

    rng = random.Random(42)
    c = TestClient(app)
    for _ in range(300):
        n = rng.randrange(-1000, 100000)
        r = c.get(f"/u/{n}")
        if n < 0:
            assert r.status_code == 404  # [0-9]+ rejects negatives
        else:
            assert r.json() == {"uid": n, "t": "int"}
        segs = "/".join(f"s{rng.randrange(99)}" for _ in range(rng.randrange(1, 4)))
        assert c.get(f"/f/{segs}").json() == {"p": segs}


def test_fuzz_schema_never_raises_unexpected():
    from ikarem import Field, Schema, ValidationError

    class M(Schema):
        name: str = Field(..., min_length=1, max_length=5)
        age: int = Field(0, ge=0, le=120)
        email: str = Field("a@b.co", email=True)

    rng = random.Random(7)
    atoms = ["x", "", "a@b.co", "nope", 0, -1, 999, None, [], {}, True, 3.5]
    for _ in range(500):
        data = {k: rng.choice(atoms) for k in rng.sample(["name", "age", "email", "zzz"], rng.randrange(5))}
        try:
            m = M.validate(data)
            assert isinstance(m.dict(), dict)
        except ValidationError as e:
            assert e.errors
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"unexpected {type(e).__name__}: {e} for {data!r}")
