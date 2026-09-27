# Meraki → IKAREM in 5 minutes

Total diff to a running app: **2 lines**. Then spend the saved afternoon on upgrades.

## Minute 0–1: swap the package

```bash
pip uninstall meraki -y
pip install ikarem
```

IKAREM's core is zero-dependency (Meraki hard-requires uvicorn). Your `uvicorn example:app`
invocation keeps working untouched.

## Minute 1–2: swap the import

```diff
-from meraki import Meraki
-from meraki.core.response import Response
+from ikarem.meraki_compat import Meraki, MerakiResponse as Response
```

That's it. Same decorators, same `add_middleware`, same 404/405 plain-text
bodies, same byte-pair headers, same `Response(body, status_code, headers)`
with `.send()`. Run your test suite — it passes unchanged
(proven in `tests/test_meraki_compat.py`, which replays Meraki's own example
and assertions against the compat layer).

## Minute 2–3: API map (for when you touch a file anyway)

| Meraki | IKAREM native |
|---|---|
| `Response(body=b"...", status_code=201)` | same shape, or just `return {...}, 201` |
| `request.path`, `request.method` | same, plus `request.query`, `cookies`, `path_params`, `await body()/json()/form()` |
| `request.query_params` (tuples) | `request.query` (dict) |
| `request.headers` (byte pairs) | `request.headers` (lowercased dict) |
| `router` static paths only | `{uid:int/float/uuid/path}` converters, 404 vs 405, `url_for` |
| `pipeline.add(mw)` | `app.use(...)`: CORS, security headers, rate limit, sessions, CSRF |
| plugins/ (empty), config/ (empty) | working `Plugin` system + layered `Config` + `DatabasePlugin` |
| — | `Schema` validation, JWT + RBAC, `Depends()` DI, OpenAPI + `/docs`, MCP tools, `ikarem check` |

Meraki's `Request` never sees the body (it doesn't store `receive`), so any
endpoint reading POST data is new capability, not ported code.

## Minute 3–4: cash in (highest ROI first)

1. **Path params.** Meraki has none — `GET /users/{uid:int}` starts working
   the moment you use the compat layer; `request.ikarem.path_params` inside
   old handlers, native params in new ones.
2. **Return dicts.** Replace `Response(body=json.dumps(...).encode(), ...)`
   with `return {...}` — content-type and length handled for you.
3. **Kill boilerplate validation** with `Schema` + `Field()`; invalid bodies
   become 400s automatically, and OpenAPI documents them.
4. **Run `ikarem check myapp:app`** — circular deps and bad `Depends()`
   surface before traffic does.

## Minute 4–5: go native per-file

When you edit a handler, drop the compat import for that file:

```python
from ikarem import Ikarem

app = Meraki().ikarem  # the engine underneath your compat app — same object
```

`Meraki().ikarem` exposes the full `Ikarem` app: `include_router`,
`exception_handler`, `mount_static`, `websocket`, `mcp_tools()`. Migrate
handler-by-handler; both styles coexist on one app. Genuinely finished when
`grep -r meraki_compat` is empty.

## Worked example

[`../ledger/`](../ledger/) is a complete production app (auth + dashboard +
CRUD + uploads + JSON API + tests + Dockerfile) built exactly this way:
compat-shaped handlers first, native power where it pays.
