# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/). Releases are cut from Conventional Commits.

## [Unreleased]

### Added
- `SpikeManager` (`ikarem/resilience.py`, exported): the intelligent
  bulkhead — AIMD adaptive concurrency (+1 per fast response, x0.9 per
  slow one, bounded by min/max), bounded queue with loop-safe polling
  instead of instant 503s (`queue_timeout=0` keeps fail-fast),
  `exempt_paths`/`exempt()` so probes are never shed, `snapshot()` for
  dashboards, bad config refused with the fix in the message. A network
  load balancer is deliberately out of scope (nginx/cloud-LB/k8s own
  that); this is the per-process admit/queue/shed/adapt half. Tests in
  `tests/test_strong.py`.
- Hot-path pass, zero-dep intact: `Request.query`/`cookies` parsed once
  and cached (compiled plans hit them per-param), middleware 0/1 fast
  paths + snapshot dispatch (no per-level type checks, `call_next(None)`
  safe), `orjson` fast path when installed with stdlib fallback
  (`dumps_json_bytes`/`loads_json_bytes`), pre-encoded response
  content-types. Covered in `tests/test_hotpath.py`.
- Ledger serves `/mcp` (negotiated summary + transport covered live over
  real uvicorn: login, tools/list, authed tools/call).
- MCP over Streamable HTTP: `app.mount_mcp("/mcp")` serves the same
  server (tools, prompts, resources) to remote clients — POST JSON-RPC
  (single/batch, 202 on notifications-only), GET SSE stream, stateless
  (no sessions), protocol errors on HTTP 200, malformed envelopes on
  400/415. Deploy behind HTTPS for ChatGPT plugin backends.

### Fixed
- MCP tool calls ran handlers directly, bypassing the middleware stack:
  session-authed routes 500'd (`'Request' object has no attribute
  'session'`). Tool execution now runs the full pipeline (sessions,
  CSRF, rate limits, request IDs) with identical rendering — tool
  behavior matches HTTP behavior by construction.
- WebSocket path params: `@app.websocket("/ws/{room}")` captures into
  `ws.path_params` with HTTP converters; unknown converters fail at
  registration. Regression test in `tests/test_strong.py`.
- `docs/CHATGPT.md`: copy-paste GPT instructions (laws, auth shapes,
  test discipline, self-review checklist) + Actions wiring via
  `/openapi.json`, so ChatGPT writes IKAREM code that passes `check`.
- HERMES (`agent/`): versatile BYOK AI agent on the IKAREM core — ReAct loop
  over files/shell/web/memory tools, dark-glass chat UI with streaming
  (`/ws/chat`) and approval cards, 8 providers (OpenAI/Anthropic/OpenRouter/
  Groq/Gemini/Ollama/LM Studio/custom, stdlib urllib only, keys stay in the
  browser), workspace jail + shell allowlist, SQLite sessions/memories, and
  MCP tools (`hermes_chat/read/list/web_fetch/recall`) + prompts. Run with
  `ikarem run agent.app:app`; 10 tests in `agent/tests/`.
- Seams not rewrites: pydantic models accepted wherever a `Schema` goes
  (handler bodies, MCP tools) via duck-typing — zero import cost, no
  dependency. `bench/bench_validate.py` publishes the honest number
  (8.3x on 7µs/body); `tests/test_differential.py` + CI job prove
  verdict/value agreement with pydantic and both token directions with
  PyJWT (test-only deps, never runtime).
- Coverage floor: 83% on `ikarem/` measured, CI `coverage` job fails
  under 80%. Uncovered code is live-server drivers + the superseded
  `di.py` fallback, not neglected paths.
- JWT wire-format vector: fully hand-rolled HS256 token (compact JSON,
  fixed 2100 expiry) verified end to end — any implementation can
  cross-check the exact bytes.
- Industry-grade ops: `TracingMiddleware` (OTel server spans, W3C
  propagation, `ikarem[otel]` lazy extra, injectable tracer so tests run
  SDK-free) and `RedisRateLimitMiddleware` (shared per-IP windows across
  processes over any `CacheBackend`, same 429 contract, thundering-herd
  approximation stated).
- `docs/DEFAULTS.md`: every security-relevant default stated with its
  reason and override (validation strictness, cookie flags, no response
  filtering, X-Forwarded-For trust, secret refusal, HS256 limits).
- Framework knowledge as MCP (`ikarem/knowledge.py`): `ikarem mcp
  ikarem.knowledge:app` teaches agents to write IKAREM code — embedded
  quickref (no checkout needed), doc reader, repo search, live API
  reference, runnable examples, `ikarem_audit` running the framework's
  own check against candidate code, plus `ikarem_new_app`/`ikarem_review`
  prompts; usage errors raise so they surface as MCP `isError`.
  7 tests in `tests/test_knowledge.py`.

### Changed
- Cookbook recipes 4–6 match the guide: hashed passwords, CSRF tokens,
  session rotation, explicit token expiries.
- Migration guides label timelines as estimates (no production
  migrations behind them yet); FastAPI deltas warn about blocking ORM
  calls; `PHASE1.md` test count corrected.
- MCP beyond routes: `@app.tool` (plain sync/async functions as tools,
  Schema params validate, request-scoped params refused at registration),
  `@app.prompt` (`prompts/list` + `get`, str or message-list returns),
  `readOnlyHint` on safe GET reads (nothing claimed otherwise), custom
  names override route tools, prompts capability advertised only when
  present.
- SEO depth: FAQPage schema from the index FAQ, reference page intro +
  per-section cross-links ("Keep going" hub), author byline in meta +
  JSON-LD + footers + README + `pyproject authors` (takes effect next
  PyPI release).

## [1.2.1] — 2026-10-02

### Changed
- Packaging metadata only (no code changes): PyPI tagline rewritten
  (was the stale "Meraki reversed" line AI summaries quote), plus
  keywords, classifiers, and project URLs (homepage, docs, repo,
  changelog, issues) so indexes describe the current framework.

## [1.2.0] — 2026-10-02

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

## [1.1.0] — never published (folded into 1.2.0 above)

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
