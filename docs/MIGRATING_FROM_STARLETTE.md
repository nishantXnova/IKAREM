# Migrating from Starlette

Estimated cost: hours (no production migrations behind this yet). Starlette is the closest cousin — same ASGI boundary,
same response names, same TestClient. You are mostly swapping the router
spelling and gaining DI, validation, and auth you used to hand-roll.

No auto-shim; the mapping is close enough that you won't miss one.

## Map

| Starlette | IKAREM |
|---|---|
| `Route("/users/{uid:int}", endpoint)` | `@app.get("/users/{uid:int}")` |
| `JSONResponse`, `PlainTextResponse`, `RedirectResponse` | Same names |
| `request.path_params["uid"]` | `uid: int` handler param (or same dict access) |
| `request.query_params` | `request.query` |
| `Middleware(...)` classes | `app.use(mw)` (function or class) |
| `TestClient(app)` | `TestClient(app)` |
| `lifespan=` / `on_startup` | `on_startup` / `on_shutdown` + lifespan |
| `StaticFiles` mount | `app.mount_static(url, dir)` (escape-proofed) |
| `WebSocket` endpoint class | `@app.websocket(path)` + `ws.accept()` |
| Background tasks | `BackgroundTasks` param |

## Deltas that bite

- IKAREM handlers take deserialized params directly; keep `request.*` access
  where you like it — both styles coexist per handler.
- Starlette has no DI/validation/auth: those are new code, not ports. Add
  `Schema` bodies where you previously parsed `await request.json()`.
- `url_path_for` → `router.url_for(name, **params)`.
- Middleware order matters identically (onion); sessions must precede CSRF
  in both — IKAREM refuses to boot otherwise (`validate_config`).

## Plan

1. Swap the router file; keep handler bodies, change only signatures.
2. Replace manual `await request.json()` parsing with `Schema` params.
3. Add auth (`BearerAuth`, sessions) where Starlette had middleware stubs.
4. `ikarem check myapp:app` before the first deploy.

## Landing snippet (executed in CI)

```python
from ikarem import Ikarem, JSONResponse
from ikarem.testing import TestClient

app = Ikarem(enable_docs=False)


async def timing(req, call_next):
    resp = await call_next(req)
    resp.headers["x-took"] = "fast"
    return resp


app.use(timing)


@app.get("/users/{uid:int}")
async def get_user(req, uid: int):
    return JSONResponse({"uid": uid})


c = TestClient(app)
r = c.get("/users/7")
assert r.json() == {"uid": 7} and r.headers["x-took"] == "fast"
assert app.router.url_for("get_user", uid=7) == "/users/7"
```
