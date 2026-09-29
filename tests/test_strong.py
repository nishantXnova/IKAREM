"""Strong batch: trusted hosts, CSP, timeouts, bulkheads, idempotency,
body caps, latency metrics, transactions, rooms, typed package."""

import asyncio

from ikarem import (
    ConcurrencyLimitMiddleware,
    IdempotencyMiddleware,
    Ikarem,
    Room,
    SecurityHeadersMiddleware,
    ServiceUnavailable,
    TimeoutMiddleware,
    TrustedHostMiddleware,
)
from ikarem.testing import TestClient
from ikarem.websocket import WebSocket


def test_trusted_host_exact_wildcard_reject():
    app = Ikarem(enable_docs=False)
    app.use(TrustedHostMiddleware(["example.com", ".svc.internal"]))

    @app.get("/")
    async def h(req):
        return {"ok": True}

    c = TestClient(app)
    assert c.get("/", headers={"host": "example.com"}).status_code == 200
    assert c.get("/", headers={"host": "a.svc.internal"}).status_code == 200
    assert c.get("/", headers={"host": "svc.internal"}).status_code == 200
    bad = c.get("/", headers={"host": "evil.com"})
    assert bad.status_code == 400 and "not trusted" in bad.json()["detail"]
    assert c.get("/", headers={"host": "example.com.evil.com"}).status_code == 400


def test_csp_opt_in_only():
    app = Ikarem(enable_docs=False)
    app.use(SecurityHeadersMiddleware(content_security_policy="default-src 'self'"))

    @app.get("/")
    async def h(req):
        return {"ok": True}

    r = TestClient(app).get("/")
    assert r.headers["content-security-policy"] == "default-src 'self'"

    plain = Ikarem(enable_docs=False)
    plain.use(SecurityHeadersMiddleware())

    @plain.get("/")
    async def h2(req):
        return {"ok": True}

    assert "content-security-policy" not in TestClient(plain).get("/").headers


def test_timeout_middleware_503_with_retry_after():
    app = Ikarem(enable_docs=False)
    app.use(TimeoutMiddleware(timeout=0.05, retry_after=7))

    @app.get("/slow")
    async def slow(req):
        await asyncio.sleep(5)
        return {"never": True}

    @app.get("/fast")
    async def fast(req):
        return {"ok": True}

    c = TestClient(app)
    r = c.get("/slow")
    assert r.status_code == 503 and r.headers["retry-after"] == "7"
    assert c.get("/fast").status_code == 200


def test_concurrency_bulkhead_and_release():
    import threading

    app = Ikarem(enable_docs=False)
    app.use(ConcurrencyLimitMiddleware(limit=1))
    entered, release = [], threading.Event()

    @app.get("/hold")
    async def hold(req):
        import asyncio as _aio

        entered.append(1)
        await _aio.to_thread(release.wait, 10)
        return {"ok": True}

    holder_status = []
    t = threading.Thread(target=lambda: holder_status.append(TestClient(app).get("/hold").status_code))
    t.start()
    import time as _t

    for _ in range(200):
        if entered:
            break
        _t.sleep(0.05)
    assert entered == [1]  # holder occupies the single slot
    _t.sleep(0.2)
    r = TestClient(app).get("/hold")
    assert r.status_code == 503 and r.headers["retry-after"] == "1"
    release.set()
    t.join(timeout=10)
    assert holder_status == [200]
    assert TestClient(app).get("/hold").status_code == 200  # slot released
    assert entered == [1, 1]  # holder + the freed-slot check, nothing extra


def test_idempotency_replay_and_passthrough():
    app = Ikarem(enable_docs=False)
    app.use(IdempotencyMiddleware())
    calls = []

    @app.post("/pay")
    async def pay(req):
        calls.append(1)
        body = await req.json()
        return {"charged": body.get("amount", 0)}, 201

    @app.get("/boom")
    async def boom(req):
        raise RuntimeError("x")

    c = TestClient(app)
    h = {"headers": {"idempotency-key": "k-1"}}
    r1 = c.post("/pay", body={"amount": 5}, **h)
    r2 = c.post("/pay", body={"amount": 5}, **h)
    assert (r1.status_code, r2.status_code) == (201, 201)
    assert r1.json() == r2.json() and len(calls) == 1  # executed once
    r3 = c.post("/pay", body={"amount": 5}, headers={"idempotency-key": "k-2"})
    assert r3.status_code == 201 and len(calls) == 2  # new key: new charge
    assert c.post("/pay", body={"amount": 5}).status_code == 201  # no key: normal
    assert c.get("/boom").status_code == 500  # errors never cached as replays


def test_app_body_cap_and_per_call_override():
    app = Ikarem(enable_docs=False, max_body_bytes=10)

    @app.post("/echo")
    async def echo(req):
        return {"n": len(await req.body())}

    @app.post("/big-ok")
    async def big(req):
        return {"n": len(await req.body(max_bytes=1000))}

    c = TestClient(app)
    assert c.post("/echo", body="x" * 9).status_code == 200
    assert c.post("/echo", body="x" * 11).status_code == 413
    assert c.post("/big-ok", body="x" * 100).status_code == 200


def test_metrics_latency_fields():
    from ikarem.observability import _METRICS, MetricsMiddleware

    app = Ikarem(enable_docs=False)
    app.use(MetricsMiddleware())

    @app.get("/")
    async def h(req):
        return {"ok": True}

    c = TestClient(app)
    c.get("/")
    text = c.get("/metrics").text
    assert "ikarem_latency_avg_ms" in text and "ikarem_latency_max_ms" in text
    assert _METRICS["requests"] >= 2


def test_transactions_commit_and_rollback():
    async def go():
        from ikarem.db import SQLiteConnector

        db = SQLiteConnector("sqlite:///:memory:")
        await db.connect()
        await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        tx = db.transaction()
        await tx.__aenter__()
        await db.execute("INSERT INTO t (v) VALUES (?)", "kept")
        await tx.commit()
        tx2 = db.transaction()
        await tx2.__aenter__()
        await db.execute("INSERT INTO t (v) VALUES (?)", "gone")
        await tx2.rollback()
        rows = await db.fetch_all("SELECT v FROM t")
        assert [r["v"] for r in rows] == ["kept"]
        try:
            async with db.transaction():
                await db.execute("INSERT INTO t (v) VALUES (?)", "ctx-gone")
                raise RuntimeError("abort me")
        except RuntimeError:
            pass
        rows = await db.fetch_all("SELECT v FROM t")
        assert [r["v"] for r in rows] == ["kept"]
        await db.disconnect()

    asyncio.run(go())


def test_room_broadcast_join_leave_prune():
    async def go():
        room = Room()
        assert len(room) == 0

        def fake_ws(log):
            async def send_text(t):
                log.append(("text", t))

            async def send_json(d):
                log.append(("json", d))

            class W:
                pass

            w = W()
            w.send_text = send_text
            w.send_json = send_json
            return w

        a_log, b_log = [], []
        a, b = fake_ws(a_log), fake_ws(b_log)
        await room.join(a)
        await room.join(b)
        assert len(room) == 2
        assert await room.broadcast("hi", exclude=a) == 1
        assert a_log == [] and b_log == [("text", "hi")]
        assert await room.broadcast({"n": 1}) == 2
        assert b_log[-1] == ("json", {"n": 1})
        room.leave(a)
        assert len(room) == 1

        async def failing_send_text(t):
            raise RuntimeError("dead")

        class D:
            pass

        d = D()
        d.send_text = failing_send_text
        d.send_json = failing_send_text
        await room.join(d)
        assert await room.broadcast("x") == 1  # dead pruned, survivor got it
        assert len(room) == 1
        assert isinstance(WebSocket, type)

    asyncio.run(go())


def test_testclient_websocket_echo():
    app = Ikarem(enable_docs=False)

    @app.websocket("/echo")
    async def echo(ws):
        await ws.accept()
        await ws.send_text("echo:" + await ws.receive_text())

    async def go():
        c = TestClient(app)
        sent = await c.ws_connect("/echo", [{"text": "hi"}])
        assert {"type": "websocket.accept"} in sent
        assert {"type": "websocket.send", "text": "echo:hi"} in sent
        missing = await c.ws_connect("/nope", [])
        assert missing == [{"type": "websocket.close", "code": 4404}]

    asyncio.run(go())


def test_service_unavailable_exported():
    assert ServiceUnavailable.status_code == 503

    from ikarem import abort

    app = Ikarem(enable_docs=False)

    @app.get("/down")
    async def down(req):
        abort(503, "deploying")

    r = TestClient(app).get("/down")
    assert r.status_code == 503 and r.json() == {"detail": "deploying"}
