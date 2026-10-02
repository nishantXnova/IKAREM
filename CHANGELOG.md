# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/). Releases are cut from Conventional Commits.

## [Unreleased]

### Added
- Devtools with teeth: `check` AST-audits handler source — f-string /
  `.format()` / `%` SQL in db calls and blocking `time.sleep` /
  `requests.*` warn with remedies (warnings, never errors: allowlisted
  identifiers can't use `?`). Unwraps Blueprint/MethodView handlers.
- `check --strict` (warnings fail), `check --format json`,
  `inspect --format openapi` (spec to stdout),
  `inspect --format auth` (per-route public/auth inventory),
  `mcp --list` (tools without serving).
- `forge/` third showcase: workshop OS (jobs, kanban, crew chat over
  `Room`, wiki, ledger + CSV, habits, team invites via queue, settings
  with JWT/API keys, NISH-first `/api/*` with triple auth + idempotent
  writes). 15 hermetic tests; CI runs it; README showcases all three.
- Crawlable site: `/robots.txt`, `/sitemap.xml`, `/llms.txt`, `/og.png`
  routes (all 404'd before), current lastmods, PDF in sitemap, JSON-LD
  on every page, twitter descriptions, `llms.txt` footer links.
- README: keyword-rich intro, contents, NISH Mode section with runnable
  snippet, refreshed feature table and layout.
- NISH responses (`ikarem/nish.py`: stdlib-only `to_nish` writer,
  `NISHResponse`, `negotiate` for `?format=nish`/Accept); Ledger
  `/api/summary` negotiates so the NISH Viewer extension paints it.
  Output verified against both real NISH engines; `docs/NISH.md`.
- NISH full duplex: `from_nish` core reader (engine-agreed on 29 edge
  cases, 2 documented differences), `await req.nish()` (blank → None,
  malformed → 400 with line), content-hash ETags + `ConditionalMiddleware`
  (304s), `Config.load_nish()`, `/openapi.nish` on every app.
- NISH Mode: `app.nish = True` (or `nish_mode(config=...)`) converts
  every JSON response app-wide on request, etags both shapes, keeps
  errors in NISH, loads config files — one-way, idempotent, tested.
- `docs/GUIDE.md`: "From Zero to Production", 25 chapters in 5 parts
  from install to internals, each runnable and self-asserting, all
  executed in CI (`tests/test_guide.py`); published as `site/guide.html`
  (+ nav, `/guide` route, sitemap) and `site/guide.pdf` (35 pages,
  print stylesheet, `/guide.pdf` route). Login chapter hashes with pbkdf2
  (dummy-hash timing cover), env-or-raise secret guard, login rate
  limit, logout, and asserted cookie flags. Broadcast and static-guard
  chapters assert the real historical bugs (second-client receipt,
  symlink + sibling-prefix).
- `WebSocketDisconnect` (`RuntimeError` subclass — old catches keep
  working) and `app.scheduler()` public accessor.
- Migration guides for FastAPI, Starlette, Litestar, Flask, Django
  (`docs/MIGRATING_FROM_*.md`, each with a CI-executed landing snippet in
  `tests/test_migration.py`); `site/migrate.html` is now a hub for all six.
- `SECURITY.md` (supported versions, private reporting); README
  "Project basics" (Security, Contributing, Changelog, SemVer,
  deprecation policy, CI matrix).

### Fixed
- Blueprint routes with typed params, DI, Schema bodies, or BackgroundTasks
  returned 500: the hook wrapper's `functools.wraps` leaked the original
  signature into the compiled plan, so the wrapper was called with the
  original's kwargs. The wrapper now carries `__signature__` and forwards
  resolved kwargs (single resolution, hooks intact). Regression test in
  `tests/test_flask_takes.py`.

### Changed
- Site redesign: editorial type system (serif display, sans body, mono
  code only), framed hero mark with caption, rules-and-measure layout,
  tabular stats, sticky scroll-aware sidebar intact, reduced-motion
  respected. Content and anchors unchanged.
- Site layout pass: one 1240px grid shared by topbar, sidebar, content,
  and footer; sidebar and content share a top baseline; tables get header
  rows and calm dividers; topbar nav scrolls instead of wrapping on mobile.
- Site calm pass: wider section rhythm, muted eyebrows and link
  underlines (red reserved for hover and single accents), unboxed stats
  band, quieter sidebar/buttons, airier tables and footer. Dark-styled
  scrollbars (no native white bars) + `color-scheme: dark`.
- README + site headline: "Zero-dependency Python ASGI framework, built
  for humans and LLMs"; Meraki origin kept as a footnote; unexplained
  third-party badge removed; examples use typed handler params
  (`get_user(req, uid: int)`); layout lists every module once.
- `docs/COOKBOOK.md`: 20 copy-paste recipes, each runnable and
  self-asserting, all executed in CI (`tests/test_cookbook.py`);
  published as `site/recipes.html` (+ nav, `/recipes` route, sitemap).
- `docs/ECOSYSTEM.md` extension registry (`ikarem-<name>` naming,
  interface checklist, shipped/wanted/third-party tables, runnable
  plugin skeleton executed in `tests/test_docs.py`).
- OpenAPI emits `apiKeyAuth` (with per-route header name) alongside
  `bearerAuth`; compiled plans + `describe_app` carry `auth.header/scopes`;
  MCP tool text names the API-key header instead of Bearer.
- `RedisCache` shared `CacheBackend` (`ikarem[redis]`, lazy import, JSON
  values, `client=` injection for tests); single-process ceiling documented
  on `cache` (`MemoryCache`/rate-limit/`Room`/idempotency default).
- `SchedulerPlugin` lifespan wiring (`app.register(SchedulerPlugin())` —
  starts on startup, cancels on shutdown; `autostart=False` for manual
  `start_scheduler` control).
- `ikarem worker --queue` flag (was `AttributeError`); trusted-publisher
  `publish.yml` (OIDC, tag-gated, no long-lived PyPI token); CI scaffold
  smoke job (templates stay runnable) + bench smoke.

### Fixed
- `bench/bench_switch.py` crashed with `ModuleNotFoundError` when the
  rival isn't installed (every CI runner): now prints labeled
  IKAREM-only rows, exit 0; full duel when `meraki` is importable.
  Covered in `tests/test_bench.py`.

## [1.1.0] — 2026-09-30

### Added
- `ikarem inspect` compact route manifest (token-efficient LLM context).
- MCP `resources/list` + `resources/read` (`ikarem://openapi.json`,
  `ikarem://manifest`); capabilities now advertise tools + resources.
- `site/llms.txt` one-page framework manual; `AGENTS.md` agent laws.

### Added
- Startup validation: default/missing secrets refused when auth routes exist,
  `validate_config()` middleware hook (sessions, CSRF order), debug route-table log.
- `ikarem check` flags duplicate (method, path) routes as errors.
- 404s suggest close matches ("Did you mean: /users?").
- `app.resource()` validated paginated CRUD (owner-scoped, spoof-proof).
- `app.mount_asgi()` raw-subtree escape hatch.
- `deprecated()` helper (version, removal, replacement in the warning).
- `docs/PLUGINS.md` extension guide; README snippets execute in CI (`tests/test_docs.py`).
- `Scheduler` job failures are logged with tracebacks, not just counted.

### Changed
- Probes mount even with `enable_docs=False`; mount helpers fail loudly.
- Rate limiter keys direct connections by scope client IP, not one shared bucket.
- `WebSocket.receive_text` skips handshake frames, raises on disconnect.
- String annotations (`from __future__ import annotations`) resolve via type hints.

### Added
- `Schema(extra="forbid")` sanitization (unexpected keys become 400s).
- `XMLResponse` / `dict_to_xml` / `escape_html`.
- `APIKeyAuth` (static keys or async `lookup=`), `require_scopes()` (JWT
  scope/scp claims), `require_if()` predicate guard (ABAC-lite).
- Versioned migrations (`Migrator`, `migrations/NNNN_name.sql` up/down,
  `ikarem migrate up|down|status|new`).
- Durable task queue (`Queue`, `QueuePlugin`, `@task`, `ikarem worker`):
  portable leases, exponential-backoff retries, parked dead jobs.
- Cron scheduler (`app.cron` / `app.every`, explicit `start_scheduler`,
  fake-clock testable `tick()`).

### Added (strong batch)
- `TimeoutMiddleware` (503 + Retry-After), `ConcurrencyLimitMiddleware`
  (bulkhead via non-blocking acquire), `IdempotencyMiddleware`
  (`Idempotency-Key` replays over any `CacheBackend`).
- `TrustedHostMiddleware` (exact + wildcard allowlist), opt-in CSP on
  `SecurityHeadersMiddleware`, `ServiceUnavailable` (503).
- `Ikarem(max_body_bytes=...)` global body cap; latency avg/max in `/metrics`.
- Real SQLite transactions (depth-tracked auto-commit, context-manager safe).
- `Room` websocket pub/sub + `TestClient.ws_connect` in-process WS driving.
- `py.typed` marker for downstream type checkers.

### Added
- `Blueprint` (prefixed groups with own before/after hooks + error handlers,
  namespaced `url_for`, zero per-request overhead).
- `Templates` (Jinja2 via `ikarem[jinja]`, autoescaped, lazy optional import).
- `flash()` / `get_flashed_messages()` (session-backed one-shot notifications).
- `MethodView` (class-based views, full DI per method via compiled plans).
- `abort(status, detail)` (terse errors through the normal pipeline).

## [1.0.0] — 2026-09-27

Stability promise: SemVer from here. `0.x` was the proving ground; `1.x`
keeps the public API (`ikarem/__init__.py` exports, ASGI behavior, CLI
commands) backward-compatible. Provenance for the promise below.

Proven since 0.3.0 (all verified, not claimed):
- Sustained load: ~75k requests over real uvicorn (healthz 1567 rps,
  authed SQLite reads 1178 rps, concurrent writes 991 rps), zero 5xx,
  zero timeouts — plus a slow-client trickle POST. Harness: `bench/load.py`.
- Live Postgres: connector CRUD + full Ledger suite green against a real
  server; loop-tolerant pools; `min_size=1` stranded-connection hygiene.
- SQLite: serialized shared connection (fixed cross-thread corruption),
  WAL mode, honest single-lane tradeoff documented.
- Hardening: bounded cache/rate-limit tables, static realpath guard,
  streaming body caps, DB-aware `/readyz`, websocket handshake fix.
- 108 tests green across CPython 3.10–3.13 × Linux/macOS/Windows (CI),
  ruff lint + format clean.

### Added
- DB-aware `/readyz` readiness probe (503 when the database is down).
- `Request.body(max_bytes)` streaming cap; `Request.json()` capped at 10MB
  by default.

### Changed
- Probes (`/healthz`, `/readyz`, `/metrics`) mount even with
  `enable_docs=False`; only `/openapi.json` + `/docs` honor the flag.
- `MemoryCache(maxsize)` and `RateLimitMiddleware(max_buckets)` evict
  expired entries and bound table growth.

### Fixed
- Static file symlink escape + prefix-collision bypass (realpath guard).
- Multipart crash on non-latin-1 content-types (now 413).
- `WebSocket.receive_text` consumed the connect handshake; disconnects now
  raise instead of returning empty strings.

## [0.3.0] — 2026-09-27

### Added
- Signed-cookie sessions (`SessionMiddleware`) + double-submit CSRF (`CSRFMiddleware`).
- HTML forms + multipart uploads (`Request.form()`, `UploadFile`, 413 caps) and
  `Schema` validation of form bodies, not just JSON.
- `Field()` constraints (`ge/le/gt/lt/min_length/max_length/pattern/email`,
  descriptions) surfaced in validation errors, OpenAPI, and MCP schemas.
- `Response.set_cookie/delete_cookie`; `FileResponse` carries status, headers,
  and cookies through middleware.
- Routes-as-MCP-tools (`app.mcp_tools/mcp_call/mcp_server`, `ikarem mcp`).
- Meraki drop-in (`ikarem.meraki_compat.Meraki`) + `docs/MIGRATING_FROM_MERAKI.md`
  + `bench/bench_switch.py` with honest `bench/RESULTS.md`.
- `ikarem new` production starter generator; `ledger/` showcase finance app.
- `ikarem check` static handler audit; O(1) static-route fast path.
- Error responses render inside the middleware pipeline (request-ID, security,
  CORS, and rate-limit headers on 4xx/5xx too).

### Changed
- `license` metadata to SPDX `MIT` string.

### Fixed
- `FileResponse` was stringified by `to_response` (missing `status_code`).
- Single-`Schema`-param handlers misclassified as legacy request-only.
- `TestClient` mutated caller-supplied headers (stale-cookie freeze).
- Ledger demo seed missing the `Rent` category (seed aborted early).

## [0.2.0] — 2026-09-23

- Compiled DI plans (zero per-request reflection), OpenAPI 3.1 + `/docs`,
  JWT/RBAC auth, cache, background tasks, websockets, static mounts,
  observability (`/healthz`, `/metrics`), multi-engine `db/` strategies.
