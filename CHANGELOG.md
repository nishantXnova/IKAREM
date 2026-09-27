# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/). Releases are cut from Conventional Commits.

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
