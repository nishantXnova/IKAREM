# Meraki vs IKAREM — benchmark (IKAREM rows refreshed 2026-10-14, this machine, `python bench/bench_switch.py`)

In-process ASGI, N=3000 sequential requests, same harness, same process.
No network, no DB — pure framework overhead.

| route | app | status | req/s |
|---|---|---|---|
| GET /hello (static) | meraki | 200 | ~314,000 (2026-09-27; rival not installed today) |
| GET /hello (static) | ikarem | 200 | ~131,000 (was ~110,000 on 2026-09-27) |
| GET /hello (static) | ikarem-compat | 200 | ~72,000 |
| GET /missing (404) | meraki | 404 | ~347,000 (2026-09-27) |
| GET /missing (404) | ikarem | 404 | ~52,000 |
| POST /hello (405) | meraki | 405 | ~328,000 (2026-09-27) |
| POST /hello (405) | ikarem | 405 | ~91,000 |
| GET /users/{id} (params) | ikarem | 200 | ~90,000 (was ~85,000) — meraki: no path params (404) |
| POST /echo 1KB JSON | ikarem | 200 | ~67,000 (same noise band as ~71,000) — meraki: Request has no body API |

Same-machine A/B for the 2026-10-14 hot-path pass (stashed before/after,
minutes apart): static +12–24%, params +12–16%, echo +12–15%, 404/405
flat. Meraki rows are 2026-09-27 (rival not installed on this machine
today); IKAREM deltas are same-day comparisons, never cross-era.

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

## Single-body validation (this machine, `python bench/bench_validate.py`, refreshed 2026-10-14)

Batch shootouts always favor Rust and never matter — frameworks validate
one body per request. Same shape, same coercions:

| validator | valid | invalid |
|---|---|---|
| ikarem `Schema.validate` | 3.53 µs/body (was 7.41) | 3.33 µs/body (was 6.70) |
| pydantic `model_validate` 2.13.4 | 0.89 µs/body | 1.16 µs/body |

Ratio: 8.3x → **4.0x** — the hot-path pass caches per-class field plans
(MRO walks and FieldInfo scans ran per request; now once per class) and
calls the compiled plan directly (two lazy imports + a delegating frame
per request, gone). cProfile: −12% wall on a mixed static/param/JSON
workload, −22% function calls. Same honesty as before: real gap,
irrelevant absolute cost next to millisecond-scale request I/O. And since pydantic models are accepted
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

## Supply-chain footprint (this machine, 2026-10-08, `python bench/footprint.py`)

One isolated venv per framework, `pip install` from PyPI (ikarem from this
repo), best-of-5 cold `import` in fresh processes:

| framework | version | packages pulled | installed | cold import |
|---|---|---|---|---|
| ikarem | 1.3.0 | 1 (ikarem) | 905 kB | 146 ms |
| fastapi | 0.143.0 | 11 (annotated-doc, annotated-types, anyio, fastapi, idna, opentelemetry-api, pydantic, pydantic-core, …) | 14,410 kB | 328 ms |
| flask | 3.1.3 | 7 (blinker, click, Flask, itsdangerous, Jinja2, MarkupSafe, Werkzeug) | 4,840 kB | 228 ms |
| django | 5.2.18 | 4 (asgiref, Django, sqlparse, tzdata) | 31,435 kB | 52 ms |

## Reading it honestly

The axis that matters here is not milliseconds — it is **what you ship**:
1 package vs 4–11, 0.9 MB vs 5–31 MB. Every pulled package is code you
didn't write running in your process: CVEs, breakage, and install time
you inherit sight unseen. (FastAPI's own tree recently grew
opentelemetry-api — fine software, but it arrived in every FastAPI
install unannounced.)

Two concessions, stated plainly. First, cold import is *not* IKAREM's
win: `import django` costs 52 ms because Django lazily defers the world
to `django.setup()`; IKAREM eagerly initializes ~40 stdlib-only modules
(~87 ms of the 146, the rest is interpreter spawn, same for every row).
Import milliseconds are noise next to first-request work on every
framework — don't over-read the column. Second, rival dependencies buy
real ecosystems: pydantic validation, Jinja, an ORM. The argument was
never "dependencies are bad"; it is "don't pay for what you don't
use." IKAREM's middle path is lazy extras — `ikarem[postgres]` installs
asyncpg if and only if you ask.

Reproducible end to end: `python bench/footprint.py` builds the venvs,
pins nothing (rival versions are whatever PyPI serves that day, printed
in the table), and takes a few minutes.
