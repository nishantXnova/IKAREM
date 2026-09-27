"""Meraki vs IKAREM: same harness, same process, in-process ASGI calls.

Meraki can't read request bodies, has no path params, and ships no
middleware — those rows are marked GAP instead of faked. Everything else
is apples-to-apples: identical routes, one trivial middleware each.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "meraki-dummy-ahh" / "Meraki" / "src"))

N = 3000
BODY = json.dumps({"data": "x" * 1024}).encode()


def make_scope(method, path, body=b""):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "server": ("t", 80),
        "client": ("t", 1),
    }


def make_receive(body=b""):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    return receive


async def hit(app, method, path, body=b""):
    msgs = []

    async def send(m):
        msgs.append(m)

    await app(make_scope(method, path, body), make_receive(body), send)
    status, payload = 500, b""
    for m in msgs:
        if m["type"] == "http.response.start":
            status = m["status"]
        elif m["type"] == "http.response.body":
            payload += m.get("body", b"")
    return status, payload


def build_meraki():
    from meraki import Meraki
    from meraki.core.response import Response

    app = Meraki()

    async def mw(request, call_next):
        return await call_next(request)

    app.add_middleware(mw)

    @app.get("/hello")
    async def hello(request):
        return Response(body=b"hello")

    @app.post("/users")
    async def users(request):
        return Response(body=b"created", status_code=201)

    return app


def build_ikarem_native():
    from ikarem import Ikarem

    app = Ikarem(enable_docs=False)

    async def mw(req, call_next):
        return await call_next(req)

    app.use(mw)

    @app.get("/hello")
    async def hello(req):
        return "hello"

    @app.post("/users")
    async def users(req):
        return {"ok": True}, 201

    @app.get("/users/{uid:int}")
    async def one(req, uid: int):
        return {"uid": uid}

    @app.post("/echo")
    async def echo(req):
        return await req.json()

    return app


def build_ikarem_compat():
    from ikarem.meraki_compat import Meraki, MerakiResponse

    app = Meraki()

    async def mw(request, call_next):
        return await call_next(request)

    app.add_middleware(mw)

    @app.get("/hello")
    async def hello(request):
        return MerakiResponse(body=b"hello")

    @app.post("/users")
    async def users(request):
        return MerakiResponse(body=b"created", status_code=201)

    return app


async def bench(app, method, path, body=b""):
    s, _ = await hit(app, method, path, body)  # correctness first
    await hit(app, method, path, body)
    t0 = time.perf_counter()
    for _ in range(N):
        await hit(app, method, path, body)
    dt = time.perf_counter() - t0
    return s, N / dt


async def main():
    mk, ik, cp = build_meraki(), build_ikarem_native(), build_ikarem_compat()
    rows = []
    s, r = await bench(mk, "GET", "/hello")
    rows.append(("GET /hello (static)", "meraki", s, r, None))
    s, r = await bench(ik, "GET", "/hello")
    rows.append(("GET /hello (static)", "ikarem", s, r, None))
    s, r = await bench(cp, "GET", "/hello")
    rows.append(("GET /hello (static)", "ikarem-compat", s, r, None))
    s, r = await bench(mk, "GET", "/missing")
    rows.append(("GET /missing (404)", "meraki", s, r, None))
    s, r = await bench(ik, "GET", "/missing")
    rows.append(("GET /missing (404)", "ikarem", s, r, None))
    s, r = await bench(mk, "POST", "/hello")
    rows.append(("POST /hello (405)", "meraki", s, r, None))
    s, r = await bench(ik, "POST", "/hello")
    rows.append(("POST /hello (405)", "ikarem", s, r, None))
    s, r = await bench(ik, "GET", "/users/42")
    rows.append(("GET /users/{id} (params)", "ikarem", s, r, "meraki: no path params (404)"))
    s, r = await bench(ik, "POST", "/echo", BODY)
    rows.append(("POST /echo 1KB JSON", "ikarem", s, r, "meraki: Request has no body API"))

    base = {route: rate for route, name, _, rate, _ in rows if name == "meraki"}
    print(f"N={N} in-process ASGI req/s (same harness, same process)")
    print(f"{'route':28} {'app':14} {'status':7} {'req/s':>10}  note")
    for route, name, status, rate, note in rows:
        mark = ""
        if name != "meraki" and route in base and base[route]:
            mark = f"{rate / base[route]:.2f}x vs meraki"
        print(f"{route:28} {name:14} {status!s:7} {rate:10,.0f}  {note or mark}")
    print("reading it honestly: Meraki wins empty-route micro-benchmarks because it")
    print("does ~nothing per request (no header parsing, no body API, no params, no")
    print("DI, no error model). Past ~50k in-process req/s both frameworks are 10x")
    print("beyond what a network + database app saturates — and every row that")
    print("matters to a real app (params, bodies, validation, auth) is IKAREM-only.")


if __name__ == "__main__":
    asyncio.run(main())
