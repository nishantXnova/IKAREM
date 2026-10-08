# IKAREM

**Zero-dependency Python ASGI framework, built for humans and LLMs.**

<img src="assets/ikarem-logo.svg" width="420" alt="IKAREM logo — a reversed K slashed through, over mirrored MERAKI, also slashed">

[![CI](https://github.com/nishantXnova/IKAREM/actions/workflows/ci.yml/badge.svg)](https://github.com/nishantXnova/IKAREM/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ikarem)](https://pypi.org/project/ikarem/)
[![Python](https://img.shields.io/pypi/pyversions/ikarem)](https://pypi.org/project/ikarem/)
[![License](https://img.shields.io/pypi/l/ikarem)](https://github.com/nishantXnova/IKAREM/blob/main/LICENSE)

FastAPI-style DX (DI, validation, OpenAPI, auth) + Django/Nest-style structure
(plugins, config, RBAC) + a core that runs on stdlib alone. Pip-installable,
**zero required dependencies**.

A Python web framework for REST APIs, HTML apps, websockets, background jobs,
and LLM tool servers — one package from prototype to production.

- [Install](#install) · [30-second example](#30-second-example) ·
  [Industry-grade example](#industry-grade-example) ·
  [Production web apps](#production-web-apps) ·
  [Flask's best, taken](#flasks-best-taken) ·
  [Ops batch](#ops-batch-migrations-queues-cron-api-keys) · [NISH Mode](#nish-mode-one-switch) ·
  [Extensions](#extensions-self-pentest--oauth2) ·
  [Stronger](#stronger-timeouts-bulkheads-idempotency-rooms) ·
  [Switching](#switching-bring-your-routes) ·
  [Why IKAREM](#why-ikarem) · [Verification](#verification) ·
  [Layout](#layout) · [Project basics](#project-basics)

## Install

```bash
pip install ikarem                    # v1.4.0 from PyPI, zero dependencies
pip install "ikarem[server,test]"     # uvicorn + pytest + httpx for dev
pip install "ikarem[postgres]"        # asyncpg strategy (also: mysql, sqlserver, redis, otel, jinja)
```

From source instead:

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
async def get_user(req, uid: int):
    return {"uid": uid}


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


class Note(Schema):
    text: str = Field(..., min_length=1, max_length=500)


@app.post("/notes")
async def add(req, note: Note):  # validates JSON *and* HTML form bodies
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
`@app.tool` exposes plain functions and `@app.prompt` exposes message
templates over the same protocol; safe GET reads carry `readOnlyHint`.
`app.mount_mcp("/mcp")` serves it all over Streamable HTTP (stateless)
for remote clients like ChatGPT plugin backends.

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
from ikarem.db import DatabasePlugin

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
Out of scope on purpose: full ORM (strategy + validated schemas is the answer).
OAuth2/social login lives one install away: [`ikarem-oauth`](#extensions-self-pentest--oauth2)
(refresh rotation + Google/GitHub code flow) — core JWT bearer covers service auth.

## Stronger: timeouts, bulkheads, idempotency, rooms

```python
from ikarem import TimeoutMiddleware, ConcurrencyLimitMiddleware, IdempotencyMiddleware, TrustedHostMiddleware

app.use(TimeoutMiddleware(30))  # hung handler -> 503 + Retry-After
app.use(ConcurrencyLimitMiddleware(100))  # bulkhead: fail fast past N in-flight
app.use(IdempotencyMiddleware())  # Idempotency-Key replays, no double charges
app.use(TrustedHostMiddleware(["example.com", ".example.com"]))
```

Plus `Ikarem(max_body_bytes=...)` DoS floor, latency fields in `/metrics`,
atomic SQLite transactions, `Room` pub/sub for websockets (tested via
`TestClient.ws_connect`), and a `py.typed` marker so downstream type checkers
see the real types.

## NISH Mode: one switch

```python
from ikarem import Ikarem
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)
app.nish_mode()  # or app.nish = True


@app.get("/hello")
async def hello(req):
    return {"hello": "ikarem"}


c = TestClient(app)
assert c.get("/hello").json() == {"hello": "ikarem"}
nish = c.get("/hello", query="format=nish")
assert nish.body.startswith(b"NISH/1.0") and "etag" in nish.headers
```

One switch and the whole API negotiates NISH (`?format=nish` or `Accept`),
both shapes get content-hash ETags with automatic 304s, errors speak NISH,
`await req.nish()` parses bodies, `/openapi.nish` serves the contract, and
`app.nish_mode(config="app.nish")` loads typed config. Full story:
[`docs/NISH.md`](docs/NISH.md).

Live proof it all works, three apps deep: [`ledger/`](ledger/) — a
personal-finance app (auth, dashboard with SVG charts, CRUD, receipt
uploads, CSV export, JSON API); [`cadence/`](cadence/) — a habit tracker
(streaks, heatmaps); [`forge/`](forge/) — a workshop OS (jobs, kanban,
crew chat, ledger, habits, NISH-first API). All running on stock IKAREM
+ uvicorn.

## Extensions: self-pentest + OAuth2

Basics are built in (`ikarem audit` grades your deployment, core JWT
covers service auth). The heavy tools install separately — outside
`ikarem/`, zero new core dependencies, each with a documented removal
path:

```bash
pip install ikarem-pentest   # after first PyPI publish; today: pip install ./extensions/ikarem-pentest
ikarem-pentest myapp:app --strict --serve --all-routes
```

**ikarem-pentest** fires real attacks at your own routes — auth bypass,
reflected XSS, SQL-injection smoke, open redirects, body-cap floods,
traceback leaks — in-process in seconds, plus the Nuclei engine
(8000+ templates) for live servers. Measured 7/7 recall on a planted
vuln gym, zero false positives. CI gates (`--strict`), allowlists
(`--ignore`), token auth, Markdown reports.
[Pentest page →](https://ikarem.vercel.app/extensions/pentest)

```python
from ikarem.db import DatabasePlugin
from ikarem_oauth import OAuthPlugin, github_provider

app.register(DatabasePlugin("sqlite:///app.db"))
app.register(OAuthPlugin(auth_secret="...",
    provider=github_provider("ID", "SECRET"), on_user=find_or_create))
```

**ikarem-oauth** adds Google/GitHub login (OAuth2 code flow, PKCE S256,
single-use state) and opaque refresh tokens done right: single-use
rotation, replay kills the chain, revocation, RFC 6749 errors, `jti` +
`scopes` in access JWTs. Stdlib only.
[OAuth page →](https://ikarem.vercel.app/extensions/oauth)

## Switching? Bring your routes

Meraki (5 minutes):

```diff
-from meraki import Meraki
+from ikarem.meraki_compat import Meraki
```

Your app runs unchanged (same routes, middleware, 404/405 bodies) on the
IKAREM engine — then migrate handler-by-handler. From anywhere else:

- FastAPI (a day): [`docs/MIGRATING_FROM_FASTAPI.md`](docs/MIGRATING_FROM_FASTAPI.md)
- Starlette (hours): [`docs/MIGRATING_FROM_STARLETTE.md`](docs/MIGRATING_FROM_STARLETTE.md)
- Litestar (a day): [`docs/MIGRATING_FROM_LITESTAR.md`](docs/MIGRATING_FROM_LITESTAR.md)
- Flask (a day): [`docs/MIGRATING_FROM_FLASK.md`](docs/MIGRATING_FROM_FLASK.md)
- Django (a week, views rewrite): [`docs/MIGRATING_FROM_DJANGO.md`](docs/MIGRATING_FROM_DJANGO.md)
- Meraki (full guide): [`docs/MIGRATING_FROM_MERAKI.md`](docs/MIGRATING_FROM_MERAKI.md)

Plus honest benchmarks: [`bench/RESULTS.md`](bench/RESULTS.md) ·
installable extensions: **self-pentest scanner** (`pip install ./extensions/ikarem-pentest` — auth bypass, XSS, SQLi probes)
and **OAuth2 login** (`pip install ./extensions/ikarem-oauth` — Google/GitHub + refresh rotation) —
[hub](https://ikarem.vercel.app/extensions) ·
extension registry: [`docs/ECOSYSTEM.md`](docs/ECOSYSTEM.md) ·
20 runnable recipes: [`docs/COOKBOOK.md`](docs/COOKBOOK.md) ·
From Zero to Production guide: [`docs/GUIDE.md`](docs/GUIDE.md) ([PDF](https://ikarem.vercel.app/guide.pdf)) ·
NISH responses for the Viewer extension: [`docs/NISH.md`](docs/NISH.md) ·
install: `pip install ikarem`.

*Footnote: IKAREM started as an answer to [Meraki](https://github.com/sulfurcodes/Meraki)
— same decorator shape, working plugins, and everything Meraki's README
promised but never shipped. The rivalry is archived; this section is just the
moving van.*

## Why IKAREM

| Rival feature | IKAREM answer |
|---|---|
| pip package, minimal deps | **Zero required deps.** `uvicorn`/`asyncpg`/etc are optional extras. Core is pure stdlib + ASGI. Measured: 1 package / 905 kB vs 11 / 14 MB (FastAPI), 4 / 31 MB (Django) — [`bench/RESULTS.md`](bench/RESULTS.md). |
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
| Validation (Pydantic-lite) | `Schema` models from type hints: coercion, required/optional, nested models, `ValidationError` → 400. Zero deps — and pydantic models are accepted as body params too (duck-typed seam, 8.3x on 7µs either way: `bench/bench_validate.py`). |
| Auth | Stdlib HS256 JWT (algorithm-confusion resistant, `sub` required, expiry enforced), pbkdf2 passwords, `BearerAuth` + `APIKeyAuth`, `require_roles()` / `require_scopes()` / `require_if()`. |
| Caching | `MemoryCache` + `RedisCache` + `@cached` over a swappable `CacheBackend` interface. |
| Background work | `BackgroundTasks` param (after send) + durable `Queue` (survives deploys) + cron. |
| Realtime / files | `app.websocket(path)` + `Room` pub/sub + `WebSocket` helper; `mount_static()` + `FileResponse`. |
| NISH | `app.nish_mode()`: whole-API negotiation, ETags + 304s, `req.nish()`, `/openapi.nish`, NISH config. |
| Observability | JSON logging, `x-request-id` + `x-process-time-ms`, `/healthz` + `/readyz` + `/metrics`, OTel tracing (`ikarem[otel]`), shared Redis rate limits. |

## Verification

439 passed, 3 skipped — framework plus showcases plus extensions, one command:

```bash
python -m pytest tests/ ledger/tests cadence/tests forge/tests relay/tests \
  extensions/ikarem-pentest/tests extensions/ikarem-oauth/tests -q
```

CI runs the same suite on Python 3.10–3.13 × Ubuntu/macOS/Windows, plus a
live-Postgres job, a Docker showcase build, a starter-template smoke job
(`ikarem new` + template tests), ruff lint + format, and
`ikarem check ledger.app:app`. Every behavior ships with a test; bugfix PRs
include a regression test. `check` also AST-audits handler source —
f-string/`.format()`/`%` SQL and blocking calls warn with remedies
(the interpreter accepts both; production regrets both). Gate harder
with `check --strict`, emit machine output with `check --format json`,
review exposure with `inspect --format auth`, dump the spec with
`inspect --format openapi`, and list tools with `mcp --list`.

## Layout

```
ikarem/            zero-dep stdlib core (v1.4.0) — optional integrations lazy-load behind extras
  app.py          Ikarem core + ASGI callable + lifespan + background/cleanup wiring
  compiled.py     one-time handler plans (no per-request reflection) + check/describe IR
  routing.py      compiled routes + converters + Did-you-mean 404s
  http.py         Request (+forms/uploads) + Response family (+cookies)
  di.py           Depends + nesting + cycle detection + run_cleanups
  validation.py   Schema models + Field() constraints (zero-dep validation)
  auth.py         JWT + passwords + Bearer/API-key auth + roles/scopes guards
  session.py      signed-cookie sessions + CSRF
  security.py     CORS + security headers + trusted hosts + rate limiting (+ Redis)
  resilience.py   timeouts + bulkheads + idempotency
  cache.py        CacheBackend + MemoryCache + RedisCache + @cached
  tracing.py      TracingMiddleware (OTel spans, SDK lazy via ikarem[otel])
  nish.py         NISH writer/reader + NISHResponse + negotiate (see NISH Mode)
  conditional.py  ConditionalMiddleware (ETag 304s)
  db/             DatabaseConnector ABC + sqlite/postgres/mysql/sqlserver + plugin + factory
  queue.py        durable task queue + QueuePlugin + `ikarem worker`
  scheduler.py    cron/intervals + SchedulerPlugin
  migrations.py   versioned migrations + `ikarem migrate`
  resources.py    app.resource() validated CRUD
  blueprints.py   prefixed route groups + MethodView (views.py)
  middleware.py   Middleware base + stack
  plugins.py      Plugin protocol + dependency-sorted PluginManager
  config.py       layered Config + NISH config files
  errors.py       HTTPException hierarchy + handler registry
  openapi.py      OpenAPI 3.1 builder + /openapi.json + /openapi.nish + /docs
  mcp.py          routes-as-MCP-tools + resources + stdio server
  websocket.py    WebSocket disconnects + Room pub/sub + WSRouter
  static.py       FileResponse + escape-proof static mounts
  templating.py   Jinja2 via ikarem[jinja] + flashing.py one-shot messages
  observability.py  logging + request-ID + metrics + /healthz + /readyz + /metrics
  cli.py          `ikarem run|check|mcp|new|migrate|worker|inspect` (+ audit.py source checks)
  testing.py      TestClient (cookie jar, all verbs, WS driving — no server needed)
  scaffold.py     `ikarem new` starter generator
  deprecation.py  deprecated() upgrade path + meraki_compat.py drop-in shim
tests/ + ledger/tests + cadence/tests + forge/tests + relay/tests + extensions/*/tests   439-test suite (see Verification)
ledger/ + cadence/ + forge/   production showcase apps (finance, habits, workshop OS)
relay/                        incident + status hub (28 routes: JWT/RBAC/API keys, SpikeManager ingest lane, nitro rollups, live feed, cron probes, MCP, debug pulse)
extensions/ikarem-pentest (active self-pentest: 6 probes + Nuclei layer + vuln gym) + ikarem-oauth (refresh rotation + PKCE code flow)
examples/basic.py    minimal CRUD + DB plugin app
bench/            honest benches (bench_switch.py) + sustained-load proof (load.py)
docs/             COOKBOOK.md (20 runnable recipes) · GUIDE.md (25 chapters) · NISH.md ·
                  ECOSYSTEM.md (extension registry) · MIGRATING_FROM_*.md (6 frameworks) ·
                  DEFAULTS.md (every default, stated plainly) · CHATGPT.md (GPT pack) ·
                  DEPLOY.md · PUBLISHING.md (token-free PyPI releases) · PLUGINS.md · SECURITY.md · PHASE1.md (original spec)
site/             static docs site (no build step) + llms.txt framework manual
```

## Project basics

- **Security:** [`SECURITY.md`](SECURITY.md) — supported versions, how to report
  (GitHub private vulnerability reporting; no public issues for vulns).
- **Contributing:** [`CONTRIBUTING.md`](CONTRIBUTING.md) — setup, per-PR checks,
  the five agent laws (zero-dep core, tests, no per-request reflection,
  fix-saying errors, Conventional Commits).
- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md) — every release cut from
  Conventional Commits, Keep-a-Changelog format.
- **Versioning (SemVer):** `1.x` keeps the public API backward-compatible —
  `ikarem/__init__.py` exports, ASGI behavior, CLI commands, response shapes.
  Minor versions add; breaking changes wait for a major.
- **Deprecation policy:** removals land only in majors, announced at least one
  minor earlier via `deprecated(since=, removal=, use_instead=)` — upgrades
  warn with the version, the removal target, and the replacement.
- **CI:** Python 3.10–3.13 × Ubuntu/macOS/Windows, live Postgres, Docker build,
  scaffold smoke, ruff lint + format.

## Author

Built by **Nishant Paudel** in Nepal. Issues and PRs welcome at
[`github.com/nishantXnova/IKAREM`](https://github.com/nishantXnova/IKAREM).
