# IKAREM cookbook: 20 copy-paste recipes

Every snippet below is runnable and self-asserting — `tests/test_cookbook.py`
executes all 20 in CI. If a recipe drifts, the suite goes red. Copy any
block into a scratch file and run it with `python <file>`.

## 1. Hello + typed path params + query defaults

Dicts become JSON. `{uid:int}` coerces or 404s; query params fill from
defaults when absent.

```python
from ikarem import Ikarem
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


@app.get("/")
async def home(req):
    return {"hello": "ikarem"}


@app.get("/users/{uid:int}")
async def get_user(req, uid: int, limit: int = 5):
    return {"uid": uid, "limit": limit}


c = TestClient(app)
assert c.get("/").json() == {"hello": "ikarem"}
assert c.get("/users/3", query="limit=2").json() == {"uid": 3, "limit": 2}
assert c.get("/users/abc").status_code == 404
```

## 2. JSON validation with constraints

`Schema` coerces types; `Field()` adds bounds; `extra="forbid"` turns
unknown keys into 400s instead of silently ignoring them.

```python
from ikarem import Field, Ikarem, Schema
from ikarem.testing import TestClient


class Item(Schema, extra="forbid"):
    name: str = Field(..., min_length=1, max_length=80)
    qty: int = Field(1, ge=1, le=99)


app = Ikarem(enable_docs=False)


@app.post("/items")
async def create(item: Item):
    return {"name": item.name, "qty": item.qty}


c = TestClient(app)
assert c.post("/items", body={"name": "apple", "qty": "2"}).json() == {"name": "apple", "qty": 2}
assert c.post("/items", body={"qty": 1}).status_code == 400
assert c.post("/items", body={"name": "x", "hack": 1}).status_code == 400
assert c.post("/items", body={"name": "x", "qty": 500}).status_code == 400
```

## 3. HTML forms + file uploads

`await req.form()` handles urlencoded and multipart. Files arrive as
`UploadFile` (filename, content_type, size); bodies over the cap 413.

```python
from ikarem import Ikarem
from ikarem.http import UploadFile
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


@app.post("/note")
async def note(req):
    form = await req.form()
    f = form.get("photo")
    if isinstance(f, UploadFile):
        return {"title": form.get("title"), "filename": f.filename, "size": f.size}
    return {"title": form.get("title"), "filename": None}


def _multipart(fields, files, boundary="BND"):
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    for k, (fn, body, ct) in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
            f"Content-Type: {ct}\r\n\r\n{body}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode(), f"multipart/form-data; boundary={boundary}"


c = TestClient(app)
r = c.post("/note", body="title=hello", content_type="application/x-www-form-urlencoded")
assert r.json() == {"title": "hello", "filename": None}
body, ct = _multipart({"title": "pic"}, {"photo": ("a.png", "BYTES", "image/png")})
r = c.post("/note", body=body, content_type=ct)
assert r.json() == {"title": "pic", "filename": "a.png", "size": 5}
```

## 4. Sessions: login, me, logout

Signed-cookie sessions. `req.session` is a dict; mutating it re-signs the
cookie. Secret comes from `session_secret=` (refused at startup when auth
routes exist and the secret is still the default).

```python
from ikarem import Ikarem, SessionMiddleware, Unauthorized
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False, session_secret="cookbook-session-secret")
app.use(SessionMiddleware())


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


@app.post("/logout")
async def logout(req):
    req.session.clear()
    return {"ok": True}


c = TestClient(app)
assert c.get("/me").status_code == 401
assert c.post(
    "/login", body="user=amy&pw=s3cret", content_type="application/x-www-form-urlencoded"
).json() == {"ok": True}
assert c.get("/me").json() == {"uid": "u1"}
assert c.post("/logout").json() == {"ok": True}
assert c.get("/me").status_code == 401
```

## 5. JWT Bearer + roles

Stdlib HS256, no deps. `require_roles()` reads the `roles` claim from the
app's `auth_secret`; 401 without a token, 403 with the wrong role.

```python
from ikarem import Depends, Ikarem, create_token, require_roles
from ikarem.testing import TestClient

SECRET = "cookbook-auth-secret"
app = Ikarem(enable_docs=False, auth_secret=SECRET)


@app.get("/admin")
async def admin(req, claims=Depends(require_roles("admin"))):
    return {"sub": claims["sub"]}


c = TestClient(app)
assert c.get("/admin").status_code == 401
user = create_token("u2", SECRET, roles=["user"])
assert c.get("/admin", headers={"authorization": f"Bearer {user}"}).status_code == 403
root = create_token("u1", SECRET, roles=["admin"])
assert c.get("/admin", headers={"authorization": f"Bearer {root}"}).json() == {"sub": "u1"}
```

## 6. API keys + scopes

Service-to-service keys via `APIKeyAuth` (static dict or async `lookup=`),
user scopes via `require_scopes()` (`scope`/`scp` JWT claims).

```python
from ikarem import APIKeyAuth, Depends, Ikarem, create_token, require_scopes
from ikarem.testing import TestClient

SECRET = "cookbook-scope-secret"
app = Ikarem(enable_docs=False, auth_secret=SECRET)
keys = APIKeyAuth({"svc-key": {"name": "billing"}})


@app.get("/internal")
async def internal(req, info=Depends(keys)):
    return {"svc": info["name"]}


@app.get("/files")
async def files(req, claims=Depends(require_scopes("read"))):
    return {"n": 2}


c = TestClient(app)
assert c.get("/internal").status_code == 401
assert c.get("/internal", headers={"x-api-key": "svc-key"}).json() == {"svc": "billing"}
tok = create_token("u1", SECRET, scope="read write")
assert c.get("/files", headers={"authorization": f"Bearer {tok}"}).json() == {"n": 2}
narrow = create_token("u2", SECRET, scope="write")
assert c.get("/files", headers={"authorization": f"Bearer {narrow}"}).status_code == 403
```

## 7. Pagination

Slice + envelope. `page`/`per_page` from query with sane clamps; always
return `total` so clients can render page counts.

```python
from ikarem import Ikarem
from ikarem.testing import TestClient

ROWS = [{"id": i} for i in range(1, 6)]
app = Ikarem(enable_docs=False)


@app.get("/rows")
async def rows(req, page: int = 1, per_page: int = 2):
    page = max(1, page)
    per_page = min(50, max(1, per_page))
    start = (page - 1) * per_page
    return {"total": len(ROWS), "page": page, "items": ROWS[start : start + per_page]}


c = TestClient(app)
p1 = c.get("/rows", query="page=1&per_page=2").json()
assert (p1["total"], p1["page"], len(p1["items"])) == (5, 1, 2)
p3 = c.get("/rows", query="page=3&per_page=2").json()
assert [r["id"] for r in p3["items"]] == [5]
```

## 8. CRUD resource in one call

`app.resource()` generates validated, paginated JSON CRUD from a Schema +
table. No owner scoping here (see `owner_field=` in `ikarem/resources.py`
for the session-scoped variant).

```python
from ikarem import Ikarem, Schema
from ikarem.db import DatabasePlugin
from ikarem.testing import TestClient


class NoteIn(Schema):
    text: str


app = Ikarem(enable_docs=False)
app.register(DatabasePlugin("sqlite:///:memory:"))


@app.on_startup
async def init():
    await app.state_db.execute(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)"
    )


app.resource("/notes", NoteIn, table="notes")

c = TestClient(app)
assert c.post("/notes", body={"text": "buy milk"}).status_code == 201
r = c.post("/notes", body={"text": "second"})
assert r.status_code == 201
lst = c.get("/notes").json()
assert lst["total"] >= 2 and lst["items"]
rid = r.json()["id"]
assert c.get(f"/notes/{rid}").json()["text"] == "second"
assert c.put(f"/notes/{rid}", body={"text": "edited"}).json()["text"] == "edited"
assert c.delete(f"/notes/{rid}").json() == {"ok": True}
assert c.get(f"/notes/{rid}").status_code == 404
```

## 9. Background tasks (fire-and-forget)

Return the response now, run the side effect after send. Gone on restart
with no retry — when the job must survive deploys, use recipe 10 instead.

```python
from ikarem import BackgroundTasks, Ikarem
from ikarem.testing import TestClient

sent = []
app = Ikarem(enable_docs=False)


@app.post("/welcome")
async def welcome(req, bg: BackgroundTasks):
    body = await req.json()
    bg.add(sent.append, body["email"])
    return {"queued": True}


c = TestClient(app)
assert c.post("/welcome", body={"email": "amy@x.com"}).json() == {"queued": True}
assert sent == ["amy@x.com"]
```

## 10. Durable queue (survives restarts)

Portable leases, exponential-backoff retries, parked dead jobs. Workers
drain via `ikarem worker myapp:app`; here `run_one()` drives a single job.

```python
import asyncio

from ikarem import Ikarem
from ikarem.db import DatabasePlugin
from ikarem.queue import QueuePlugin, task

seen = []


@task("cookbook-greet")
async def greet(name="world"):
    seen.append(name)


app = Ikarem(enable_docs=False)
app.register(DatabasePlugin("sqlite:///:memory:"))
app.register(QueuePlugin())


async def main():
    await app.startup()
    try:
        await app.state_queue.enqueue("cookbook-greet", {"name": "ada"})
        assert await app.state_queue.depth() >= 1
        assert await app.state_queue.run_one() is True
        assert seen == ["ada"]
        assert await app.state_queue.run_one() is False
    finally:
        await app.shutdown()


asyncio.run(main())
```

## 11. Cron + intervals (testable clock)

Jobs register with decorators; all timing flows through `tick(now)`, so
tests drive time instead of sleeping. Tie to lifespan with
`app.register(SchedulerPlugin())` in production.

```python
import asyncio
import time

from ikarem import Ikarem

app = Ikarem(enable_docs=False)
fired = []


@app.every(60)
async def heartbeat():
    fired.append(1)


@app.cron("@daily")
async def daily():
    fired.append("daily")


async def main():
    sched = app._scheduler()
    assert len(sched.jobs) == 2
    await sched.tick(now=time.time() + 61)
    assert fired == [1]


asyncio.run(main())
```

## 12. Middleware: timing + auth gate

`(request, call_next) -> response`. Return without calling `call_next` to
short-circuit. Function middleware and classes both work.

```python
from ikarem import Ikarem, Unauthorized
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


async def timing(req, call_next):
    resp = await call_next(req)
    resp.headers["x-took"] = "fast"
    return resp


async def gate(req, call_next):
    if req.headers.get("x-flag") != "yes":
        raise Unauthorized("flag required")
    return await call_next(req)


app.use(timing)
app.use(gate)


@app.get("/gated")
async def gated(req):
    return {"ok": True}


c = TestClient(app)
assert c.get("/gated").status_code == 401
r = c.get("/gated", headers={"x-flag": "yes"})
assert r.json() == {"ok": True} and r.headers["x-took"] == "fast"
```

## 13. Errors: abort + custom handlers

`abort(status, detail)` fails tersely through the normal pipeline (so
middleware headers still apply). `@app.exception_handler` maps exception
types, most-specific match first.

```python
from ikarem import Ikarem, abort
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


@app.exception_handler(ValueError)
async def _bad(req, exc):
    from ikarem import JSONResponse

    return JSONResponse({"detail": f"bad value: {exc}"}, status_code=422)


@app.get("/widget/{wid:int}")
async def widget(req, wid: int):
    if wid == 0:
        abort(404, "no such widget")
    return {"wid": wid}


@app.get("/boom")
async def boom(req):
    raise ValueError("wid")


c = TestClient(app)
assert c.get("/widget/0").status_code == 404
assert c.get("/boom").status_code == 422
assert c.get("/widget/7").json() == {"wid": 7}
```

## 14. CORS + security headers + trusted hosts

Preflights answer 204 without touching handlers; security headers apply to
every response including errors; unlisted `Host` values 400 immediately.

```python
from ikarem import CORSMiddleware, Ikarem, SecurityHeadersMiddleware, TrustedHostMiddleware
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)
app.use(TrustedHostMiddleware(["example.com"]))
app.use(SecurityHeadersMiddleware())
app.use(CORSMiddleware(allow_origins=["https://app.example.com"]))


@app.get("/")
async def home(req):
    return {"ok": True}


c = TestClient(app)
assert c.get("/", headers={"host": "evil.com"}).status_code == 400
r = c.get("/", headers={"host": "example.com"})
assert r.headers["x-frame-options"] == "DENY"
assert r.headers["access-control-allow-origin"] == "https://app.example.com"
pre = c.request("OPTIONS", "/", headers={"host": "example.com"})
assert pre.status_code == 204
```

## 15. Rate limiting

Fixed-window, per-IP, bounded buckets. 429s carry `Retry-After` plus
`X-RateLimit-*` headers; successful responses carry the quota headers too.

```python
from ikarem import Ikarem, RateLimitMiddleware
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)
app.use(RateLimitMiddleware(per_minute=2))


@app.get("/")
async def home(req):
    return {"ok": True}


c = TestClient(app)
assert c.get("/").status_code == 200
assert c.get("/").status_code == 200
r = c.get("/")
assert r.status_code == 429 and "retry-after" in r.headers
assert r.headers["x-ratelimit-limit"] == "2"
```

## 16. Timeouts, bulkheads, idempotent writes

Overload becomes clean 503s with `Retry-After` instead of wedges; retried
POSTs with the same `Idempotency-Key` replay instead of double-charging.

```python
import asyncio

from ikarem import ConcurrencyLimitMiddleware, IdempotencyMiddleware, Ikarem, TimeoutMiddleware
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)
app.use(TimeoutMiddleware(timeout=0.05, retry_after=7))
app.use(ConcurrencyLimitMiddleware(limit=10))
app.use(IdempotencyMiddleware())

charges = []


@app.get("/slow")
async def slow(req):
    await asyncio.sleep(5)
    return {"never": True}


@app.get("/fast")
async def fast(req):
    return {"ok": True}


@app.post("/pay")
async def pay(req):
    charges.append(1)
    return {"charged": len(charges)}


c = TestClient(app)
r = c.get("/slow")
assert r.status_code == 503 and r.headers["retry-after"] == "7"
assert c.get("/fast").status_code == 200
h = {"idempotency-key": "k-1"}
a = c.post("/pay", body={}, headers=h)
b = c.post("/pay", body={}, headers=h)
assert a.json() == b.json() == {"charged": 1} and len(charges) == 1
```

## 17. WebSocket rooms

In-process pub/sub: join on connect, broadcast to the rest, leave in a
`finally`. Single process by design — an external broker slots in behind
the same shape when you outgrow one node.

```python
import asyncio

from ikarem import Room


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


async def main():
    room = Room()
    a, b = FakeWS(), FakeWS()
    await room.join(a)
    await room.join(b)
    assert len(room) == 2
    n = await room.broadcast("hi", exclude=a)
    assert n == 1 and b.sent == ["hi"] and a.sent == []
    room.leave(a)
    room.leave(b)
    assert len(room) == 0


asyncio.run(main())
```

## 18. Static files + downloads

`mount_static` serves a directory with symlink-escape and prefix-collision
guards (`..` never leaves the root). `FileResponse` streams downloads with
`Content-Disposition` intact through middleware.

```python
import tempfile
from pathlib import Path

from ikarem import Ikarem
from ikarem.testing import TestClient

pub = Path(tempfile.mkdtemp(prefix="cookbook-static-"))
(pub / "ok.txt").write_text("hello file")

app = Ikarem(enable_docs=False)
app.mount_static("/static", str(pub))


@app.get("/receipt")
async def receipt(req):
    from ikarem import FileResponse

    return FileResponse(str(pub / "ok.txt"), filename="receipt.txt")


c = TestClient(app)
r = c.get("/static/ok.txt")
assert r.status_code == 200 and r.text == "hello file"
assert c.get("/static/../secret.txt").status_code == 404
dl = c.get("/receipt")
assert dl.status_code == 200 and "attachment" in dl.headers.get("content-disposition", "")
```

## 19. OpenAPI + routes-as-MCP-tools

Every route derives its OpenAPI operation and MCP tool from the same
compiled plan — query shapes, bodies, and auth boundaries can't drift.

```python
import asyncio

from ikarem import Depends, Ikarem, Schema, require_roles
from ikarem.openapi import build_openapi

SECRET = "cookbook-mcp-secret"


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
tools = {t["name"]: t for t in app.mcp_tools()}
assert set(tools) == {"create_item", "admin"}
out = asyncio.run(app.mcp_call("create_item", {"name": "apple"}))
assert out["isError"] is False
```

## 20. Testing + static audit

`TestClient` keeps cookies across requests (login flows just work);
`app.check()` statically audits handlers — cycles, bare `Depends()`,
duplicate routes — before traffic. 404s suggest close matches.

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
miss = c.get("/user")
assert miss.status_code == 404 and "Did you mean" in miss.text
```
