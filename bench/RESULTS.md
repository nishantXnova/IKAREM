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
