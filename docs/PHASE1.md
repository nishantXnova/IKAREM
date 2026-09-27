# IKAREM — Phase 1 (Rival Crusher)

Meraki spelled backwards. Goal: everything Meraki Phase 1 promises, but leaner,
faster, and with better DX — proven by tests, not words.

## 1. Objective
Pip-installable MVP of IKAREM: lightweight modular Python ASGI framework.
Zero required dependencies. Uvicorn/asyncpg/aiomysql/aioodbc are optional extras.

## 2. Distribution — DONE
- `pyproject.toml` (setuptools), `pip install -e .`, future-PyPI ready.
- `pip install ikarem[server]` / `[postgres]` / `[mysql]` / `[sqlserver]` / `[test]`.

## 3. HTTP/ASGI layer — DONE
- `Ikarem.__call__(scope, receive, send)` — pure ASGI, server-agnostic.
- Tested with in-process TestClient; `app.run()` uses uvicorn only when installed.
- Handlers never see ASGI: `Request` + `to_response()` abstraction.

## 4. Application core — DONE (`ikarem/app.py`)
- `Ikarem(debug, **config)` owns router, middleware, plugins, exceptions, config.
- `startup()` / `shutdown()` + lifespan protocol + `on_startup/on_shutdown` decorators.

## 5. Request/Response — DONE (`ikarem/http.py`)
- `Request`: method/path/query/headers/cookies/path_params/state, `await body()/json()/text()`.
- `Response` + `JSONResponse/TextResponse/HTMLResponse/RedirectResponse/StreamingResponse`.
- Handlers may return dict/list/str/bytes/Response/(body, status).

## 6. Routing — DONE, beats spec (`ikarem/routing.py`)
- Beyond "path matching": pre-compiled regex, `{p}`, `{p:int/float/uuid/path/str}`.
- 404 vs 405 distinction. `url_for(name, **params)` introspection.

## 7. Middleware — DONE (`ikarem/middleware.py`)
- `Middleware` onion: `async __call__(req, call_next)`.
- Global via `app.use()`, short-circuitable, per-route possible, raw-ASGI compat path.

## 8. Plugins — DONE (`ikarem/plugins.py`)
- `BasePlugin`: `name/requires/priority/register/on_startup/on_shutdown/on_request/on_response`.
- `PluginManager` topologically sorts `requires`, priority tie-break, circular detection.

## 9. Configuration — DONE (`ikarem/config.py`)
- Layered: defaults < kwargs < `load_dict` < `IKAREM_*` env. `config.get(k, default, cast=...)`.

## 10. Errors — DONE (`ikarem/errors.py` + `app.exception_handler`)
- `HTTPException` + `BadRequest/Unauthorized/Forbidden/NotFound/MethodNotAllowed/InternalError`.
- `@app.exception_handler(Exc)` registry, MRO most-specific match. Debug tracebacks only when `debug=True`.

## 11. Database extension — DONE (`ikarem/db/`)
- `DatabaseConnector` ABC: `connect/disconnect/execute/fetch_one/fetch_all/execute_many/transaction` (async).
- Strategies: `SQLiteConnector` (stdlib, works now), `PostgresConnector` (asyncpg),
  `MySQLConnector` (aiomysql), `SQLServerConnector` (aioodbc) — all lazy-import drivers.
- `create_connector(url)` factory + `DatabasePlugin(url)` proving plugin-architecture extension.

## 12. Testing — DONE (`tests/`, 13 passing)
- `test_app` (lifecycle/routing/converters/405/echo), `test_middleware` (after/short-circuit),
  `test_plugins` (hooks/deps/lifecycle), `test_db` (factory/sqlite CRUD/plugin wiring).
- `ikarem/testing.py::TestClient` — no server needed.

## 13. Docs + example — DONE
- `README.md` (30-sec example), `examples/basic.py` (CRUD + DB plugin), this file.

## 14. Completion checklist (mirrors Meraki §14)
- [x] pip-installable — `pip install -e .`
- [x] app can be created + started — `Ikarem()` + `startup()` + lifespan
- [x] uvicorn serves via ASGI — `app.run()` / `ikarem run`
- [x] routing to handlers — converters + 404/405
- [x] middleware pipeline — onion + short-circuit
- [x] plugins + lifecycle — sorted manager + hooks
- [x] config + error handling — layered + registry
- [x] DB common interface + 4 conforming impls
- [x] tests cover core
- [x] example + docs

## 15. Out of scope (same as Meraki)
No ORM, no engine-specific features leaking through the abstraction,
no prod deploy tooling beyond `app.run()`.

## Where IKAREM already crushes Meraki
1. Zero-dep core (Meraki bakes in server deps).
2. Typed path converters + 405 handling (Meraki: "path-based" only).
3. Dependency-sorted plugins with priorities (Meraki: "manager" only).
4. Layered env config (Meraki: "centralized" only).
5. Debug-gated tracebacks + handler registry (Meraki: "centralized" only).
6. Async DB interface with placeholder normalization + in-memory SQLite that actually runs in tests.
