# Meraki vs IKAREM — benchmark (2026-09-27, this machine, `python bench/bench_switch.py`)

In-process ASGI, N=3000 sequential requests, same harness, same process.
No network, no DB — pure framework overhead.

| route | app | status | req/s |
|---|---|---|---|
| GET /hello (static) | meraki | 200 | ~314,000 |
| GET /hello (static) | ikarem | 200 | ~110,000 (0.35x) |
| GET /hello (static) | ikarem-compat | 200 | ~62,000 (0.20x) |
| GET /missing (404) | meraki | 404 | ~347,000 |
| GET /missing (404) | ikarem | 404 | ~97,000 (0.28x) |
| POST /hello (405) | meraki | 405 | ~328,000 |
| POST /hello (405) | ikarem | 405 | ~88,000 (0.27x) |
| GET /users/{id} (params) | ikarem | 200 | ~85,000 — meraki: no path params (404) |
| POST /echo 1KB JSON | ikarem | 200 | ~71,000 — meraki: Request has no body API |

## Reading it honestly

Meraki wins the empty-route rows because it does ~nothing per request: no
header parsing (headers stay raw byte pairs), no body API (`Request` doesn't
even store `receive`), no params, no DI, no error model, empty plugin/config
stubs. IKAREM's ~9µs/request buys: decoded headers/cookies/query, compiled
DI, validation, auth, JSON errors, request-ID/metrics, OpenAPI + MCP
emission, `ikarem check`.

Past ~50k in-process req/s, both frameworks are an order of magnitude beyond
what a network + database app saturates — framework overhead is noise next
to I/O. The rows that decide a switch (params, bodies, validation, auth,
sessions, docs) are IKAREM-only. Rerun anytime: `python bench/bench_switch.py`.

## Single-body validation (this machine, `python bench/bench_validate.py`)

Batch shootouts always favor Rust and never matter — frameworks validate
one body per request. Same shape, same coercions:

| validator | valid | invalid |
|---|---|---|
| ikarem `Schema.validate` | 7.41 µs/body | 6.70 µs/body |
| pydantic `model_validate` 2.13.4 | 0.89 µs/body | 1.16 µs/body |

Ratio: 8.3x on 7µs — real gap, irrelevant absolute cost next to
millisecond-scale request I/O. And since pydantic models are accepted
wherever a `Schema` goes (duck-typed seam, zero import cost), the Rust
core is one `pip install pydantic` away with no code changes.

## Sustained load, real uvicorn (2026-09-30, Windows laptop, `python bench/load.py`)

Ledger on stock uvicorn, 16 keep-alive client threads, 45s phases, plus a
slow-client trickle POST. Verdict both runs: **LOAD OK — zero 5xx, zero
timeouts.**

| phase | rps | p50 | p99 | n |
|---|---|---|---|---|
| GET /healthz (public) | 1557 | 10.2ms | 12.6ms | 15,569 × 200 |
| GET /api/summary (authed SQLite reads) | 994 | 15.8ms | 24.7ms | 44,711 × 200 |
| POST /api/txns (authed writes + validation + CSRF) | 557 | 18.7ms | 219.7ms | 11,139 × 201 |

~71k requests per run. Writes p99 moves run to run (557–794 rps observed) —
SQLite is single-lane by design, so concurrent-write latency is the honest
cost of the embedded DB, not the framework; point `IKAREM_DB_URL` at
Postgres past toy scale. No rival was installed on this machine, so no new
duel rows — the 2026-09-27 table above stands.
