# Migrating from Litestar

Estimated cost: a day (no production migrations behind this yet). Litestar's layered shape (app → controller → handler)
maps cleanly onto app → `Blueprint` → route, and its signature modeling
(query params as kwargs, status codes as returns) already thinks the way
IKAREM handlers do.

No auto-shim; port controller by controller.

## Map

| Litestar | IKAREM |
|---|---|
| `@get("/users/{uid:int}")` handler | `@app.get("/users/{uid:int}")` handler |
| `Controller` with `path=` | `Blueprint(name, url_prefix=)` + `register_blueprint` |
| `Provide(dep)` DI | `Depends(dep)` (per-request cache both) |
| DTOs (`DataclassDTO`, attrs) | `Schema` + `Field()` |
| `guards=[...]` | `require_roles()` / `require_scopes()` / `require_if()` |
| Return `(data, StatusCode.CREATED_201)` | Return `(data, 201)` |
| `Request`, `Response`, `MediaType` | `Request`, `Response`, `JSONResponse` family |
| Layers (app → controller → router) | app → blueprint → route (same nesting instinct) |
| `TestClient` | `TestClient` (cookie jar included) |

## Deltas that bite

- Litestar layers enforce architecture; IKAREM `Blueprint`s are the opt-in
  version — bring the discipline with you, it isn't automatic.
- DTO backends (SQLAlchemy integration, advanced attrs types) become plain
  `Schema` validation plus the `DatabaseConnector` strategy. Port DTOs
  before handlers.
- OpenAPI emits from the same place in both (signatures), so `/docs` keeps
  working — verify operation IDs your clients codegen against.
- Litestar middleware/logging config → `app.use(...)` + `configure_logging`.

## Plan

1. One controller → one `Blueprint`; keep paths identical.
2. DTOs → `Schema` models; diff 400 responses against the old app.
3. Guards → `Depends(...)`; replay auth tests unchanged.
4. `ikarem check myapp:app` before the first deploy.

## Landing snippet (executed in CI)

```python
from ikarem import Blueprint, Ikarem
from ikarem.testing import TestClient

api = Blueprint("api", url_prefix="/api")
app = Ikarem(enable_docs=False)


@api.get("/users/{uid:int}")
async def get_user(req, uid: int):
    return {"uid": uid}, 200


app.register_blueprint(api)

c = TestClient(app)
r = c.get("/api/users/7")
assert r.status_code == 200 and r.json() == {"uid": 7}
assert app.router.url_for("api.get_user", uid=7) == "/api/users/7"
```
