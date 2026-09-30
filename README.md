# IKAREM — Meraki, reversed. And crushed.

<img src="assets/ikarem-logo.svg" width="420" alt="IKAREM logo — a reversed K slashed through, over mirrored MERAKI, also slashed">

[![CI](https://github.com/nishantXnova/IKAREM/actions/workflows/ci.yml/badge.svg)](https://github.com/nishantXnova/IKAREM/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ikarem)](https://pypi.org/project/ikarem/)
[![Python](https://img.shields.io/pypi/pyversions/ikarem)](https://pypi.org/project/ikarem/)
[![License](https://img.shields.io/pypi/l/ikarem)](https://github.com/nishantXnova/IKAREM/blob/main/LICENSE)
[![M8ven Verified](https://m8ven.ai/badge/mcp/nishantxnova-ikarem-1yam5c?variant=verified&v=f97b1e1df496f42096a0d7689cc7dad9)](https://m8ven.ai/mcp/nishantxnova-ikarem-1yam5c)

Industry-grade Python ASGI backend framework. Pip-installable, **zero-dep core**.

FastAPI-style DX (DI, validation, OpenAPI, auth) + Django/Nest-style structure
(plugins, config, RBAC) + a core that runs on stdlib alone.

## Install

```bash
pip install -e .                  # core only, zero dependencies
pip install -e ".[server,test]"   # uvicorn + pytest + httpx for dev
pip install -e ".[postgres]"      # asyncpg strategy
pip install -e ".[mysql]"         # aiomysql strategy
pip install -e ".[sqlserver]"     # aioodbc strategy
```

## 30-second example

```python
from ikarem import Ikarem

app = Ikarem(debug=True)


@app.get("/")
async def home(req):
    return {"hello": "ikarem"}  # dict auto-becomes JSON


@app.get("/users/{uid:int}")
async def get_user(req):
    return {"uid": req.path_params["uid"]}


if __name__ == "__main__":
    app.run()  # needs `pip install ikarem[server]`
```

```bash
python examples/basic.py
# or
ikarem run examples.basic:app --reload
```

## Industry-grade example

```python
from ikarem import (
    BackgroundTasks,
    Depends,
    Ikarem,
    Schema,
    CORSMiddleware,
    RateLimitMiddleware,
    create_token,
    require_roles,
)

app = Ikarem(auth_secret="secret")
app.use(CORSMiddleware())
app.use(RateLimitMiddleware(per_minute=120))


class Item(Schema):
    name: str
    qty: int = 1


def get_prefix():
    return "hi"


@app.post("/items")
async def create(req, item: Item, bg: BackgroundTasks, prefix=Depends(get_prefix)):
    bg.add(print, f"created {item.name}")
    return {"msg": f"{prefix} {item.name}", "qty": item.qty}  # validated + coerced


tok = create_token("u1", "secret", roles=["admin"])


@app.get("/admin")
async def adm(req, claims=Depends(require_roles("admin"))):
    return {"sub": claims["sub"]}  # 401 without token, 403 without role
```

Built-ins on every app: `GET /healthz`, `GET /metrics`,
`GET /openapi.json` (OpenAPI 3.1 auto-gen), `GET /docs` (Swagger UI).

## Production web apps

```bash
ikarem new myapp && cd myapp   # auth + sessions + SQLite CRUD (HTML + JSON), tests + Dockerfile
pytest -q && uvicorn app:app
```

```python
from ikarem import SessionMiddleware, CSRFMiddleware, Field, Schema

app.use(SessionMiddleware())  # signed cookies: req.session["uid"] = ...
app.use(CSRFMiddleware())  # unsafe routes need X-CSRF-Token or _csrf_token field


class Signup(Schema):
    email: str = Field(..., email=True, max_length=254)
    password: str = Field(..., min_length=8, max_length=128)


@app.post("/notes")
async def add(req, note: NoteIn):  # validates JSON *and* HTML form bodies
    form = await req.form()  # urlencoded + multipart, UploadFile files,
    f = form.get("doc")  # 413 past size caps
    await f.write(f"/uploads/{f.filename}")
```

`TestClient` keeps a cookie jar (login flows just work) and speaks
`get/post/put/patch/delete`. `ikarem check` audits handlers, `ikarem mcp`
serves every route as an LLM tool (plus `ikarem://openapi.json` and
`ikarem://manifest` MCP resources), `ikarem inspect` prints a compact route
manifest for LLM context, and `site/llms.txt` is the framework manual in one
page. `AGENTS.md` holds the contributor laws for AI and human agents alike.

## Flask's best, taken

```python
from ikarem import Blueprint, MethodView, Templates, abort, flash

api = Blueprint("api", url_prefix="/api")


@api.get("/items/{uid:int}")  # own hooks, own errors, url_for("api.x")
async def one(req, uid: int): ...


app.register_blueprint(api)  # hooks wrap once — still zero per-request reflection

abort(403, "owner only")  # terse errors through the normal pipeline


class Items(MethodView):  # one class per resource, full DI per method
    async def get(self, req): ...
    async def post(self, req, item: Item): ...


app.route("/items", Items.methods())(Items.as_view("items"))

flash(req, "Saved.")  # session-backed, shown once in templates
Templates("templates/").response("hi.html", name="amy")  # Jinja2 via ikarem[jinja]
```

Deliberately *not* taken: context locals (`g`, global `request` proxies) and signals —
explicit `req` params plus plugins and middleware cover that ground without the magic.

## Ops batch: migrations, queues, cron, API keys

```python
from ikarem import Migrator, QueuePlugin, require_scopes, APIKeyAuth, task

app.register(DatabasePlugin("postgresql://..."))
app.register(QueuePlugin())  # durable jobs in app.state_queue


@task("welcome")  # name -> callable, worker-dispatched
async def welcome(to: str): ...


@app.post("/signup")
async def signup(req):
    await req.app.state_queue.enqueue("welcome", {"to": "a@b.co"})
    return {"ok": True}, 201


@app.every(300)  # or @app.cron("0 2 * * *")
async def nightly(): ...


# await app.start_scheduler()                  # explicit: nothing runs unless started
```

```bash
ikarem migrate new add_users && ikarem migrate up myapp:app
ikarem worker myapp:app                        # drain the queue until Ctrl+C
```

Plus the small ones the checklist demanded: `Schema(extra="forbid")` sanitization,
`XMLResponse`/`dict_to_xml`, `escape_html`, `APIKeyAuth` (static keys or async `lookup=`),
`require_scopes()` for JWT scopes, `require_if()` predicate (ABAC-lite).
Out of scope on purpose: full ORM (strategy + validated schemas is the answer) and
OAuth2 dance (JWT bearer covers service auth).

## Stronger: timeouts, bulkheads, idempotency, rooms

```python
from ikarem import TimeoutMiddleware, ConcurrencyLimitMiddleware, IdempotencyMiddleware

app.use(TimeoutMiddleware(30))  # hung handler -> 503 + Retry-After
app.use(ConcurrencyLimitMiddleware(100))  # bulkhead: fail fast past N in-flight
app.use(IdempotencyMiddleware())  # Idempotency-Key replays, no double charges
app.use(TrustedHostMiddleware(["example.com", ".example.com"]))
```

Plus `Ikarem(max_body_bytes=...)` DoS floor, latency fields in `/metrics`,
atomic SQLite transactions, `Room` pub/sub for websockets (tested via
`TestClient.ws_connect`), and a `py.typed` marker so downstream type checkers
see the real types.

Live proof it all works: [`ledger/`](ledger/) — a personal-finance app
(auth, dashboard with SVG charts, CRUD, receipt uploads, CSV export, JSON API)
running on stock IKAREM + uvicorn.

## Switching from Meraki? 5 minutes

```diff
-from meraki import Meraki
+from ikarem.meraki_compat import Meraki
```

Your app runs unchanged (same routes, middleware, 404/405 bodies) on the
IKAREM engine — then migrate handler-by-handler. Full guide:
[`docs/MIGRATING_FROM_MERAKI.md`](docs/MIGRATING_FROM_MERAKI.md) ·
honest benchmarks: [`bench/RESULTS.md`](bench/RESULTS.md) ·
install: `pip install ikarem` (`dist/ikarem-1.0.0-py3-none-any.whl` builds offline).

## Why IKAREM beats Meraki Phase 1 — and the industry

| Rival feature | IKAREM answer |
|---|---|
| pip package, minimal deps | **Zero required deps.** `uvicorn`/`asyncpg`/etc are optional extras. Core is pure stdlib + ASGI. |
| ASGI + Uvicorn boundary | Strict server boundary: `Ikarem` exposes `__call__(scope, receive, send)`. Any ASGI server works (uvicorn, hypercorn, daphne). HTTP + WebSocket + lifespan. |
| Central app + lifecycle | `Ikarem()` + `on_startup` / `on_shutdown` + lifespan. Plugins hook in with priority + topological dependency order. |
| Request/Response | Lazy `Request` (query, headers, cookies, `await body()/json()`), `Response` helpers (`JSON`, `text`, `html`, `stream`, `redirect`, `File`). Handlers never touch raw ASGI. |
| Routing | **Compiled** routes with `{param}` + `{param:int/float/uuid/path}` converters, 404 vs 405 distinction, `url_for`, `include_router(prefix)`, static mounts. |
| Middleware | Onion pipeline, short-circuitable, global + composable: CORS, security headers, rate limiting (429 + `Retry-After`), request-ID/timing, metrics. |
| Plugins | `Plugin` protocol (`name`, `requires`, `priority`, `register`, `on_startup/shutdown/request/response`). Manager sorts dependencies, detects cycles. |
| Config | Layered `Config`: defaults < kwargs < dict < `IKAREM_*` env vars. Typed `config.get(key, default, cast=...)`. |
| Errors | `HTTPException` hierarchy + `@app.exception_handler(ExcType)` with MRO most-specific match. Tracebacks only when `debug=True`. |
| DB Strategy (pg/mysql/sqlite/mssql) | `DatabaseConnector` async ABC (`connect/disconnect/execute/fetch_one/fetch_all/execute_many/transaction`). Lazy driver imports. SQLite runs on stdlib today. `DatabasePlugin` proves the extension model. |
| DI (FastAPI parity) | `Depends()` with nesting, per-request cache (+ opt-out), sync/async/yield deps, finalizers guaranteed **after response send** and on the exception path, circular-dep rejection. |
| Validation (Pydantic-lite) | `Schema` models from type hints: coercion, required/optional, nested models, `ValidationError` → 400. Zero deps. |
| Auth | Stdlib HS256 JWT (algorithm-confusion resistant, `sub` required, expiry enforced), pbkdf2 passwords, `BearerAuth`, `require_roles()` RBAC. |
| Caching | `MemoryCache` + `@cached` (Redis-swappable `CacheBackend` interface). |
| Background work | `BackgroundTasks` param — runs after the response is sent, never fails the response. |
| Realtime / files | `app.websocket(path)` + `WebSocket` helper; `mount_static()` + `FileResponse`. |
| Observability | JSON logging, `x-request-id` + `x-process-time-ms`, `/healthz`, Prometheus-style `/metrics`. |

## Verification

108 tests, all green — including exhaustive branch matrices:

```
tests/test_di.py         Depends() x12 (nesting, cache on/off, sync/async/yield,
                         cleanup after send — send-order proved — after exception,
                         failure mapping, cycles)
tests/test_jwt.py        JWT x7 (valid, expired, bad sig, malformed, alg=none +
                         RS256-confusion attacks, missing sub, wrong secret)
tests/test_ratelimit.py  Rate limit x7 (limit, 60s window reset, 429 + Retry-After,
                         countdown, headers everywhere, per-IP, 10-way concurrency)
tests/test_app.py        routing, converters, 405, echo
tests/test_middleware.py after-hooks, short-circuit
tests/test_plugins.py    hooks, dep order, missing dep, lifecycle
tests/test_db.py         factory routing, SQLite CRUD, plugin lifecycle
tests/test_industry.py   validation, DI+body, RBAC, security stack, background, cache, docs
tests/test_mcp_openapi.py  requestBody/query/auth in OpenAPI, MCP list/call/errors/JSON-RPC
tests/test_forms.py        urlencoded, multipart uploads, 413 caps
tests/test_session.py      login cookies, tamper/expiry, CSRF allow/deny/exempt
tests/test_fields.py       Field() ranges/lengths/patterns/emails, json_schema output
```

```bash
python -m pytest tests/ -q   # 108 passed
```

## Layout

```
ikarem/
  __init__.py     public exports (v1.0.0)
  app.py          Ikarem core + ASGI callable + DI/background/cleanup wiring
  routing.py      compiled routes + converters
  http.py         Request (+forms/uploads) + Response family (+cookies)
  session.py      signed-cookie sessions + CSRF
  validation.py   Schema models + Field() constraints (zero-dep validation)
  middleware.py   Middleware base + stack
  plugins.py      Plugin protocol + dependency-sorted PluginManager
  config.py       layered Config
  errors.py       HTTPException hierarchy + handler registry
  validation.py   Schema models (zero-dep validation)
  di.py           Depends + cycle detection + run_cleanups
  openapi.py      OpenAPI 3.1 builder + /openapi.json + /docs
  auth.py         JWT + passwords + BearerAuth + require_roles
  security.py     CORS + security headers + rate limiting
  cache.py        CacheBackend + MemoryCache + @cached
  background.py   BackgroundTasks
  websocket.py    WebSocket + WSRouter
  static.py       FileResponse + static mounts
  observability.py  logging + request-ID + metrics + /healthz + /metrics
  db/             DatabaseConnector ABC + sqlite/postgres/mysql/sqlserver + plugin + factory
  cli.py          `ikarem run|check|mcp|new|migrate|worker|inspect` helper
  testing.py      TestClient (cookie jar, all verbs — no server needed)
  compiled.py     one-time handler plans (perf) + check/describe IR
  mcp.py          routes-as-MCP-tools + stdio server
  openapi.py      OpenAPI 3.1 builder + /openapi.json + /docs
  scaffold.py     `ikarem new` starter generator
tests/            108-test suite (see Verification)
examples/basic.py CRUD + DB plugin app
docs/PHASE1.md    Phase 1 spec (rival crusher)
```
