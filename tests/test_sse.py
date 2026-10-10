"""SSEResponse: framed event streams with retry + heartbeat, zero deps."""

import asyncio

from ikarem import Ikarem, SSEResponse, sse_format
from ikarem.testing import TestClient


def test_sse_format_frames():
    assert sse_format("hi") == b"data: hi\n\n"
    assert sse_format("a\nb") == b"data: a\ndata: b\n\n"
    assert sse_format({"t": 1}) == b'data: {"t": 1}\n\n'
    assert sse_format("x", event="tok", id="7", retry=3000) == b"event: tok\nid: 7\nretry: 3000\ndata: x\n\n"


def test_sse_end_to_end_headers_and_frames():
    app = Ikarem(enable_docs=False)

    async def gen():
        yield "one"
        yield {"event": "tok", "data": {"t": 2}}

    @app.get("/stream")
    async def stream(req):
        return SSEResponse(gen(), retry=1500)

    r = TestClient(app).get("/stream")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/event-stream"
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    assert r.body == b'retry: 1500\ndata: \n\ndata: one\n\nevent: tok\ndata: {"t": 2}\n\n'


def test_sse_default_event_and_sync_iterable():
    app = Ikarem(enable_docs=False)

    @app.get("/ev")
    async def ev(req):
        return SSEResponse(["a", "b"], event="msg")

    r = TestClient(app).get("/ev")
    assert r.body == b"event: msg\ndata: a\n\nevent: msg\ndata: b\n\n"


def test_sse_heartbeat_while_slow():
    app = Ikarem(enable_docs=False)

    async def slow():
        await asyncio.sleep(0.15)
        yield "late"

    @app.get("/slow")
    async def view(req):
        return SSEResponse(slow(), heartbeat=0.05)

    r = TestClient(app).get("/slow")
    assert r.body.count(b": heartbeat\n\n") >= 1
    assert r.body.endswith(b"data: late\n\n")
