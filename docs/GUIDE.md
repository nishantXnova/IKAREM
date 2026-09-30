# IKAREM — From Scratch to Genius

A complete path from an empty file to a production backend, and from using
the framework to understanding it. Five parts, twenty-two chapters. Every
snippet is runnable and self-asserting — `tests/test_guide.py` executes all
of them in CI, so this guide cannot drift. Copy any block into a scratch
file and run it with `python <file>`.

Conventions: handlers take `req` first; `TestClient` drives apps without a
server; SQLite runs in memory in these snippets and on disk (or Postgres)
in production. Nothing here needs more than `pip install ikarem` plus the
`server` and `test` extras for serving and testing.

## 01. First light

Install the package, create an app, return a dict. Dicts, lists, strings,
and bytes become responses automatically — ceremony is reserved for the
cases that need it.

```python
from ikarem import Ikarem
from ikarem.testing import TestClient

app = Ikarem()


@app.get("/")
async def home(req):
    return {"hello": "ikarem"}


c = TestClient(app)
r = c.get("/")
assert r.status_code == 200 and r.json() == {"hello": "ikarem"}
assert c.get("/missing").status_code == 404
```

Serve it for real with any ASGI server (`app.run()` needs
`pip install ikarem[server]`), or keep driving it in-process with
`TestClient` — every chapter below uses the client, and your test suite
should too. Notes: `debug=True` adds tracebacks to 500s (never in
production); `enable_docs=False` hides `/openapi.json` and `/docs` but
never the health probes.

## 02. Routing with intent

Routes declare converters (`{uid:int}`, plus `float`, `uuid`, `path`), so
bad segments 404 instead of crashing handlers. A path that matches nothing
is 404; a path that matches with the wrong verb is 405. Name every route
for free with the handler name, and reverse it with `url_for`.

```python
from ikarem import Ikarem
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


@app.get("/users/{uid:int}")
async def get_user(req, uid: int):
    return {"uid": uid}


c = TestClient(app)
assert c.get("/users/7").json() == {"uid": 7}
assert c.get("/users/abc").status_code == 404
assert c.post("/users/7", body={}).status_code == 405
assert app.router.url_for("get_user", uid=7) == "/users/7"
```

Notes: converters are compiled to regex once at registration; static
routes resolve in O(1). Unknown converters fail at startup with the valid
list, not on first traffic. Close misses get "Did you mean" 404s.

## 03. Bodies with boundaries

Read bodies explicitly (`await req.json()`, `await req.form()`,
`await req.body(max_bytes=...)`) so oversized payloads become 413s at a
limit you chose, not memory pressure you didn't. The app-wide
`max_body_bytes=` sets the floor; per-call `max_bytes=` overrides it.

```python
from ikarem import Ikarem
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


@app.post("/echo")
async def echo(req):
    return await req.json()


@app.post("/small")
async def small(req):
    return {"n": len(await req.body(max_bytes=10))}


c = TestClient(app)
assert c.post("/echo", body={"a": 1}).json() == {"a": 1}
assert c.post("/small", body="12345").json() == {"n": 5}
assert c.post("/small", body="x" * 100).status_code == 413
```

Notes: `Request.json()` caps at 10MB by default. Streaming uploads should
always pass an explicit cap. 413 responses still travel the middleware
pipeline, so observability headers survive them.

## 04. Validation that reads like the docs

`Schema` models coerce and check; `Field()` states bounds once and they
surface in errors, OpenAPI, and MCP schemas together. `extra="forbid"`
rejects unknown keys — the default ignores them, which is how mass
assignment sneaks in.

```python
from ikarem import Field, Ikarem, Schema
from ikarem.testing import TestClient


class Item(Schema):
    name: str = Field(..., min_length=1, max_length=80)
    qty: int = Field(1, ge=1, le=99)


class Order(Schema, extra="forbid"):
    item: Item
    express: bool = False


app = Ikarem(enable_docs=False)


@app.post("/orders")
async def create(order: Order):
    return {"name": order.item.name, "qty": order.item.qty, "express": order.express}


c = TestClient(app)
good = {"item": {"name": "apple", "qty": "2"}, "express": True}
assert c.post("/orders", body=good).json() == {"name": "apple", "qty": 2, "express": True}
assert c.post("/orders", body={"item": {"qty": 1}}).status_code == 400
assert c.post("/orders", body={"item": {"name": "x"}, "hack": 1}).status_code == 400
```

Notes: nested models validate recursively; coerced values (`"2"` to `2`)
are what the handler receives. `json_schema()` powers OpenAPI and MCP
input schemas from the same definition.

## 05. Dependencies without magic

`Depends()` declares inputs; the framework builds them per request, caches
repeats, and runs yield-dependency finalizers after the response is sent —
even on the exception path. Dependencies are plain callables: test them
without the framework.

```python
from ikarem import Depends, Ikarem
from ikarem.testing import TestClient

closed = []


def settings():
    return {"currency": "USD"}


async def ledger_conn(req):
    conn = {"open": True}
    yield conn
    closed.append("closed")


app = Ikarem(enable_docs=False)


@app.get("/balance")
async def balance(req, cfg=Depends(settings), db=Depends(ledger_conn)):
    return {"currency": cfg["currency"], "open": db["open"]}


c = TestClient(app)
assert c.get("/balance").json() == {"currency": "USD", "open": True}
assert closed == ["closed"]
```

Notes: nesting works to any depth; cycles fail at startup with the path.
`Depends(fn, use_cache=False)` opts out of the per-request cache. Bare
`Depends()` with no callable is a startup error naming the parameter.

## 06. Auth that says no clearly

Stateless JWT (HS256, stdlib only) plus role and scope guards. 401 means
anonymous, 403 means authenticated but not allowed — clients can act on
the difference. Secrets come from config and are refused at startup when
they are still defaults.

```python
from ikarem import Depends, Ikarem, create_token, require_roles, require_scopes
from ikarem.testing import TestClient

SECRET = "guide-auth-secret"
app = Ikarem(enable_docs=False, auth_secret=SECRET)


@app.get("/admin")
async def admin(req, claims=Depends(require_roles("admin"))):
    return {"sub": claims["sub"]}


@app.get("/files")
async def files(req, claims=Depends(require_scopes("read"))):
    return {"n": 2}


c = TestClient(app)
assert c.get("/admin").status_code == 401
user = create_token("u2", SECRET, roles=["user"])
assert c.get("/admin", headers={"authorization": f"Bearer {user}"}).status_code == 403
root = create_token("u1", SECRET, roles=["admin"], scope="read")
assert c.get("/admin", headers={"authorization": f"Bearer {root}"}).json() == {"sub": "u1"}
assert c.get("/files", headers={"authorization": f"Bearer {root}"}).json() == {"n": 2}
```

Notes: `verify_token` rejects algorithm confusion (`alg=none`), expired
tokens, and missing `sub`. Service-to-service keys use `APIKeyAuth` with
static keys or an async `lookup=`. Passwords hash with pbkdf2.

## 07. Sessions and the CSRF contract

Signed-cookie sessions for browsers; double-submit CSRF on unsafe routes.
`SessionMiddleware` must precede `CSRFMiddleware` — reversed order refuses
to boot instead of silently leaving you unprotected.

```python
from ikarem import CSRFMiddleware, Ikarem, SessionMiddleware, Unauthorized, csrf_token
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False, session_secret="guide-session-secret")
app.use(SessionMiddleware())
app.use(CSRFMiddleware())


@app.get("/csrf")
async def csrf(req):
    return {"t": csrf_token(req)}


@app.post("/login")
async def login(req):
    form = await req.form()
    if form.get("user") == "amy" and form.get("pw") == "s3cret":
        req.session["uid"] = "u1"
        return {"ok": True}
    raise Unauthorized("bad credentials")


@app.get("/me")
async def me(req):
    uid = req.session.get("uid")
    if not uid:
        raise Unauthorized("anonymous")
    return {"uid": uid}


c = TestClient(app)
assert c.get("/me").status_code == 401
t = c.get("/csrf").json()["t"]
h = {"x-csrf-token": t}
form = "user=amy&pw=s3cret"
assert c.post("/login", body=form, content_type="application/x-www-form-urlencoded", headers=h).json() == {
    "ok": True
}
assert c.get("/me").json() == {"uid": "u1"}
```

Notes: the `TestClient` cookie jar persists login across requests, so flows
test exactly as browsers behave. Token APIs can exempt paths instead of
sending CSRF headers.

## 08. Middleware on purpose

The onion: each layer sees the request going in and the response coming
out, or short-circuits. Order is the feature — request IDs first,
gates early, headers last. A `validate_config(app)` hook lets middleware
fail at boot with the remedy.

```python
from ikarem import Ikarem, RequestIDMiddleware, SecurityHeadersMiddleware
from ikarem.testing import TestClient

order = []
app = Ikarem(enable_docs=False)
app.use(RequestIDMiddleware())
app.use(SecurityHeadersMiddleware())


async def audit(req, call_next):
    order.append("in")
    resp = await call_next(req)
    order.append("out")
    resp.headers["x-audited"] = "yes"
    return resp


app.use(audit)


@app.get("/")
async def home(req):
    return {"ok": True}


c = TestClient(app)
r = c.get("/")
assert r.headers["x-request-id"] and r.headers["x-frame-options"] == "DENY"
assert r.headers["x-audited"] == "yes" and order == ["in", "out"]
```

Notes: raising `Unauthorized`/`Forbidden` inside middleware short-circuits
with the same rendering as handler errors. Keep middleware free of
business logic — gates and headers, nothing else.

## 09. Errors with remedies

`abort(status, detail)` fails tersely through the pipeline so error
responses keep request IDs and security headers. Custom handlers map
exception types, most-specific first. Framework law: every error names
the fix.

```python
from ikarem import Ikarem, JSONResponse, abort
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


@app.exception_handler(ValueError)
async def _bad(req, exc):
    return JSONResponse({"detail": f"bad value: {exc}"}, status_code=422)


@app.get("/widget/{wid:int}")
async def widget(req, wid: int):
    if wid == 0:
        abort(404, "no such widget")
    return {"wid": wid}


@app.get("/parse")
async def parse(req):
    raise ValueError("qty")


c = TestClient(app)
assert c.get("/widget/0").status_code == 404
assert c.get("/parse").status_code == 422
assert c.get("/widget/3").json() == {"wid": 3}
```

Notes: unhandled exceptions are 500s with no leak (tracebacks only under
`debug=True`). 404s suggest close matches. Handler errors render inside
the middleware pipeline — headers included.

## 10. Configuration without surprises

Layered and explicit: defaults, then kwargs, then dict, then `IKAREM_*`
environment variables. Typed reads with `cast=`. No settings module, no
import-time environment sniffing.

```python
from ikarem import Ikarem

app = Ikarem(enable_docs=False, debug=False, page_size=20)

assert app.config.get("page_size") == 20
assert app.config.get("missing", "fallback") == "fallback"
assert app.config.get("page_size", cast=str) == "20"
assert app.config.get("debug") is False
```

Notes: secrets (`auth_secret`, `session_secret`) also read from
`IKAREM_AUTH_SECRET` / `IKAREM_SESSION_SECRET`. Startup validation
refuses to serve authenticated routes on default secrets.

## 11. Data that survives

One connector interface, four engines. Placeholders are always `?`;
rows are plain dicts; drivers import lazily with the exact extra named on
failure. Transactions commit on clean exit and roll back on error.

```python
import asyncio

from ikarem import Ikarem
from ikarem.db import DatabasePlugin

app = Ikarem(enable_docs=False)
app.register(DatabasePlugin("sqlite:///:memory:"))


async def main():
    await app.startup()
    try:
        db = app.state_db
        await db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT)")
        async with db.transaction():
            await db.execute("INSERT INTO notes (text) VALUES (?)", "buy milk")
        rows = await db.fetch_all("SELECT * FROM notes")
        assert [r["text"] for r in rows] == ["buy milk"]
    finally:
        await app.shutdown()


asyncio.run(main())
```

Notes: SQLite serializes on a worker-side lock (single-lane by design,
WAL on); Postgres/MySQL pools rebuild per event loop. Point
`IKAREM_DB_URL` at Postgres when writes outgrow one lane — the suite is
green on both.

## 12. Work that outlives the deploy

`BackgroundTasks` fire after the response and vanish on restart. The
durable queue survives it: portable leases, exponential-backoff retries,
parked dead jobs instead of silent loss. Drain with `ikarem worker`.

```python
import asyncio

from ikarem import BackgroundTasks, Ikarem
from ikarem.db import DatabasePlugin
from ikarem.queue import QueuePlugin, task

fired, done = [], []


@task("guide-receipt")
async def receipt(order_id: int):
    done.append(order_id)


app = Ikarem(enable_docs=False)
app.register(DatabasePlugin("sqlite:///:memory:"))
app.register(QueuePlugin())


@app.post("/orders")
async def order(req, bg: BackgroundTasks):
    bg.add(fired.append, "ack")
    await req.app.state_queue.enqueue("guide-receipt", {"order_id": 7})
    return {"ok": True}


async def main():
    await app.startup()
    try:
        await app.state_queue.enqueue("guide-receipt", {"order_id": 7})
        assert await app.state_queue.run_one() is True
        assert done == [7]
    finally:
        await app.shutdown()


asyncio.run(main())

from ikarem.testing import TestClient

assert TestClient(app).post("/orders", body={}).json() == {"ok": True}
assert fired == ["ack"]
```

Notes: unknown task names and bad payloads fail the job, not the worker.
`depth()` exposes queue length for `/readyz`-style checks and dashboards.

## 13. Time, kept by the app

Cron plus intervals with an explicit start — nothing runs unless started.
All timing flows through `tick(now)`, so tests travel through time instead
of sleeping. In production, `SchedulerPlugin` ties the loop to lifespan.

```python
import asyncio
import time

from ikarem import Ikarem
from ikarem.scheduler import SchedulerPlugin

app = Ikarem(enable_docs=False)
ran = []


@app.every(60)
async def heartbeat():
    ran.append(1)


@app.cron("@daily")
async def rollup():
    ran.append("daily")


async def main():
    sched = app._scheduler()
    assert [j.name for j in sched.jobs] == ["heartbeat", "rollup"]
    await sched.tick(now=time.time() + 61)
    assert ran == [1]
    app.register(SchedulerPlugin(poll=0.01))
    await app.startup()
    assert getattr(app, "state_scheduler", None) is sched
    await app.shutdown()


asyncio.run(main())
```

Notes: overlapping runs of one job never stack — a still-running job is
skipped that tick. Failures are counted and logged with tracebacks, never
swallowed.

## 14. Talking back live

WebSocket routes plus in-process rooms: join on connect, broadcast to the
rest, leave in a `finally`. The `TestClient.ws_connect` driver feeds
scripted messages and collects everything sent.

```python
import asyncio

from ikarem import Ikarem, Room
from ikarem.testing import TestClient

room = Room()
app = Ikarem(enable_docs=False)


@app.websocket("/chat")
async def chat(ws):
    await ws.accept()
    await room.join(ws)
    try:
        while True:
            await room.broadcast(await ws.receive_text(), exclude=ws)
    except RuntimeError:
        pass
    finally:
        room.leave(ws)


async def main():
    sent = await TestClient(app).ws_connect("/chat", incoming=[{"type": "websocket.connect"}, {"text": "hi"}])
    kinds = [m["type"] for m in sent]
    assert "websocket.accept" in kinds


asyncio.run(main())
assert len(room) == 0
```

Notes: rooms are single-process by design; an external broker slots
behind the same join/broadcast shape when you outgrow one node.
`receive_text` skips handshake frames and raises on disconnect.

## 15. Files in, files out

Static directories serve with symlink-escape and prefix-collision guards;
`FileResponse` streams downloads with `Content-Disposition` surviving
middleware. Uploads arrive parsed as `UploadFile` with size caps.

```python
import tempfile
from pathlib import Path

from ikarem import FileResponse, Ikarem
from ikarem.testing import TestClient

pub = Path(tempfile.mkdtemp(prefix="guide-static-"))
(pub / "ok.txt").write_text("hello file")

app = Ikarem(enable_docs=False)
app.mount_static("/static", str(pub))


@app.get("/receipt")
async def receipt(req):
    return FileResponse(str(pub / "ok.txt"), filename="receipt.txt")


c = TestClient(app)
assert c.get("/static/ok.txt").text == "hello file"
assert c.get("/static/../secret.txt").status_code == 404
dl = c.get("/receipt")
assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
```

Notes: mount helpers fail loudly on missing directories. Probe and docs
routes never shadow your mounts — system routes mount first, yours win
ties by registration.

## 16. Structure that scales

`Blueprint`s group routes with their own hooks and error handlers;
`MethodView` puts one resource's verbs in one class with full DI per
method. Both compile to the same plans as plain routes — zero per-request
overhead for the organization.

```python
from ikarem import Blueprint, Ikarem, MethodView, Schema
from ikarem.testing import TestClient


class ItemIn(Schema):
    name: str


class Items(MethodView):
    async def get(self, req):
        return {"items": []}

    async def post(self, req, item: ItemIn):
        return {"name": item.name}, 201


api = Blueprint("api", url_prefix="/api")
app = Ikarem(enable_docs=False)


@api.get("/ping")
async def ping(req):
    return {"pong": True}


app.register_blueprint(api)
app.route("/items", Items.methods())(Items.as_view("items"))

c = TestClient(app)
assert c.get("/api/ping").json() == {"pong": True}
assert c.get("/items").json() == {"items": []}
assert c.post("/items", body={"name": "a"}).status_code == 201
assert c.post("/items", body={}).status_code == 400
assert app.router.url_for("api.ping") == "/api/ping"
```

Notes: blueprint routes are named `blueprint.handler`, so `url_for`
stays unambiguous across groups. Blueprint error handlers scope to
their routes; app handlers catch the rest.

## 17. Tests that behave like browsers

`TestClient` keeps cookies, speaks every verb, and runs startup — login
flows test exactly as browsers behave. `app.check()` audits handlers
statically: cycles, bare `Depends()`, duplicate routes, auth gaps.

```python
from ikarem import Ikarem
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


@app.get("/users")
async def users(req):
    return {"n": 1}


report = app.check()
assert report["errors"] == []
assert any(r["path"] == "/users" for r in report["routes"])
c = TestClient(app)
assert c.get("/users").json() == {"n": 1}
miss = c.get("/user")
assert miss.status_code == 404 and "Did you mean" in miss.text
```

Notes: one client per thread — loop-bound resources (pools) survive
across requests, matching production. Gate deploys on `check` in CI.

## 18. Lifespan, wired once

`on_startup` / `on_shutdown` order your boot: tables, queues, schedulers.
Plugins hook the same lifecycle with dependency sorting. ASGI lifespan
messages drive it all under a real server.

```python
import asyncio

from ikarem import Ikarem

events = []
app = Ikarem(enable_docs=False)


@app.on_startup
async def up():
    events.append("up")


@app.on_shutdown
async def down():
    events.append("down")


async def main():
    await app.startup()
    await app.startup()  # idempotent: second call is a no-op
    await app.shutdown()


asyncio.run(main())
assert events == ["up", "down"]
```

Notes: startup compiles every handler plan, validates config (secrets,
middleware order), then runs hooks and plugin startups. Shutdown reverses
plugins before app hooks.

## 19. Docs and tools for free

OpenAPI, the route manifest, and MCP tools all derive from the same
compiled plans — query shapes, bodies, and auth boundaries cannot drift
between your docs, your agents, and your runtime.

```python
import asyncio

from ikarem import Depends, Ikarem, Schema, require_roles
from ikarem.compiled import describe_app
from ikarem.openapi import build_openapi

SECRET = "guide-mcp-secret"


class Item(Schema):
    name: str


app = Ikarem(enable_docs=False, auth_secret=SECRET)


@app.post("/items")
async def create_item(item: Item):
    """Create an item."""
    return {"name": item.name}


@app.get("/admin")
async def admin(req, claims=Depends(require_roles("admin"))):
    return {"sub": claims["sub"]}


spec = build_openapi(app)
assert spec["paths"]["/items"]["post"]["summary"] == "Create an item."
assert spec["paths"]["/admin"]["get"]["security"] == [{"bearerAuth": []}]
manifest = describe_app(app)
assert manifest["count"] == 2
tools = {t["name"]: t for t in app.mcp_tools()}
assert set(tools) == {"create_item", "admin"}
out = asyncio.run(app.mcp_call("create_item", {"name": "apple"}))
assert out["isError"] is False
```

Notes: `ikarem inspect` prints the manifest for LLM context;
`site/llms.txt` is the one-page framework manual. Serve routes as tools
with `ikarem mcp myapp:app`.

## 20. Under the hood: plans, not reflection

Signatures parse once into handler plans; per-request resolution is dict
lookups. `get_plan` caches by handler identity — the same object serves
every request. That is the whole performance story, and it is checkable.

```python
from ikarem import Ikarem
from ikarem.compiled import get_plan
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


@app.get("/u/{uid:int}")
async def u(req, uid: int, limit: int = 5):
    return {"uid": uid, "limit": limit}


assert get_plan(u) is get_plan(u)
c = TestClient(app)
assert c.get("/u/3", query="limit=2").json() == {"uid": 3, "limit": 2}
```

Notes: `bench/bench_switch.py` proves the hot path with honest numbers;
`bench/load.py` proves sustained load (~75k requests, zero 5xx). Touch
the hot path and the bench decides, not adjectives.

## 21. Agents are users too

LLM clients consume the same contracts: manifest for planning, MCP tools
for calling, `llms.txt` for the manual, `AGENTS.md` for contributor laws.
Build agent-facing features by describing routes, not by special cases.

```python
from ikarem import Ikarem
from ikarem.compiled import describe_app, describe_route

app = Ikarem(enable_docs=False)


@app.get("/orders/{oid:int}")
async def order(req, oid: int, verbose: bool = False):
    """Fetch one order."""
    return {"oid": oid}


(route,) = [r for r in app.router.routes if r.path == "/orders/{oid:int}"]
desc = describe_route(route)
assert desc.path_params == ["oid"] and desc.doc.startswith("Fetch one")
entry = describe_app(app)["routes"][0]
assert entry["handler"] == "order" and entry["summary"] == "Fetch one order."
```

Notes: auth boundaries propagate into tool descriptions, so agents see
`Requires Authorization` before they call. Keep docstrings to one true
first line — it becomes the tool summary.

## 22. Genius is maintenance

The framework stays fast and honest the same way your app does: every
behavior ships with a test, errors name remedies, deprecations warn with
versions and replacements, and the changelog is cut from commits. Your
next moves: claim a row in `docs/ECOSYSTEM.md`, steal a recipe from
`docs/COOKBOOK.md`, and read `CONTRIBUTING.md` before the first PR.

```python
from ikarem import Ikarem, deprecated


@deprecated("use total_v2() instead", since="1.1.0", removal="2.0.0", use_instead="total_v2")
def total(items):
    return sum(items)


def total_v2(items):
    return sum(items)


import warnings

app = Ikarem(enable_docs=False)


@app.get("/total")
async def total_h(req):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        n = total([1, 2, 3])
    assert any("1.1.0" in str(w.message) and "total_v2" in str(w.message) for w in caught)
    return {"n": n}


from ikarem.testing import TestClient

assert TestClient(app).get("/total").json() == {"n": 6}
```
