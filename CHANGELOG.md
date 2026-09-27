# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/). Releases are cut from Conventional Commits.

## [Unreleased]

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
