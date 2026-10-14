# PROOF — every claim, its evidence, its gaps

No adjectives. Each row: what we claim, what proves it (file + run),
and what is still honestly open. Updated 2026-10-14.

| # | Claim | Evidence | Status |
|---|---|---|---|
| 1 | Routing + middleware core behavior | `tests/` routing/converters/404-vs-405/mounts/static-guard; `bench_switch.py` params+bodies rows | PROVEN in-process. Open: ecosystem maturity (only our apps exercise it) |
| 2 | Validation + OpenAPI correctness | `tests/test_differential.py` + CI `differential` job (verdict/value agreement vs pydantic, both JWT directions); OpenAPI/MCP schema tests | PROVEN. Pydantic drift caught by agreement tests, not hope |
| 3 | Auth adversarial + secure defaults | `audit_report` JWT-01 proves live (exp-less/alg=none/tampered rejected); pentest gym 7/7 recall, 0 FP (`extensions/ikarem-pentest/tests/test_detection.py`); OAuth PKCE/rotation/reuse tests; `docs/DEFAULTS.md` states every default | PROVEN. Maintenance via SemVer + CHANGELOG, not promises |
| 4 | WebSockets/streaming disconnects | WS handshake/disconnect tests, `Room` pub/sub, `TestClient.ws_connect`; SSE framing/heartbeat/midstream-close tests (`tests/test_sse.py`) | PROVEN in-process. Open: slow-TCP-consumer backpressure — TCP applies it, no app-level queue; untested, stated |
| 5 | Cache/queue failure recovery | Lease/retry/park (`test_ops.py`); dead-worker lease expiry + two-worker no-duplicate split (`tests/test_recovery.py`) | PROVEN single-node. Open: multi-process against real Postgres (SQLite serializes by design); Redis-backed multi-worker unproven |
| 6 | Production resilience | Bulkhead/SpikeManager/timeout/idempotency tests (`test_strong.py`); sustained load `bench/load.py` 2026-09-30: ~75k reqs, zero 5xx | PROVEN, but load numbers predate 1.4.0 fixes — refresh scheduled, not faked |
| 7 | MCP + agent safety | Routes-as-tools incl. middleware-stack fix regression; HERMES showcase (jail, allowlist, approvals) | PROVEN in-protocol. Open: real-client interop (Claude/GPT) untested here; tool permissions inherit route auth (tested) |
| 8 | CLI + observability contracts | Rich fallback pinned (`test_rich_out.py`), cp1252 verified live, terminal headless-tested (`test_terminal.py`), metrics latency fields | PROVEN. Wanted, not claimed: shell completions |
| 9 | DB transactions/pooling/parity | SQLite tx commit/rollback tests; loop-rebuild pools; **live Postgres suite green in CI** (`test_postgres_live.py`) | PROVEN SQLite + Postgres. Open: MySQL/SQLServer have no live runs (no servers in CI) — proposal: docker-service jobs like postgres |

Reading rule: a row is PROVEN only with a file that fails when the
behavior breaks. Everything else is a gap with an owner, not a roadmap
vibe.
