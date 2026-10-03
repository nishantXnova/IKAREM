# Migrating from FastAPI

Estimated cost: a day (no production migrations behind this yet). The decorator shape is already yours — the port is
mostly mechanical: Pydantic models become Schemas, FastAPI Depends becomes
`Depends`, and `TestClient` keeps its name.

No auto-shim exists (unlike `meraki_compat`). You move handler by handler;
both apps can run side by side behind `app.mount_asgi()` during the move.

## Map

| FastAPI | IKAREM |
|---|---|
| `@app.get("/users/{uid}")` + `uid: int` | `@app.get("/users/{uid:int}")` + `uid: int` |
| `class Item(BaseModel)` | `class Item(Schema)` + `Field()` |
| `item: Item` body param | `item: Item` body param (same position) |
| `x=Depends(fn)` | `x=Depends(fn)` (per-request cache both) |
| `BackgroundTasks` param | `BackgroundTasks` param (same name) |
| `JSONResponse(...)` | `JSONResponse(...)` (same name) |
| `OAuth2PasswordBearer` + scopes | `BearerAuth` + `require_roles()` / `require_scopes()` |
| `HTTPException(status, detail)` | `HTTPException` / `abort(status, detail)` |
| `TestClient(app)` | `TestClient(app)` (cookie jar included) |
| `/docs` + `/openapi.json` | `/docs` + `/openapi.json` (same paths) |

## Deltas that bite

- Path converters live in the route (`{uid:int}`), not just the annotation.
  FastAPI reads the type; IKAREM compiles the route once from both.
- No `response_model` filtering — return exactly what the client should see.
- Sync handlers run on the event loop (no hidden threadpool) - and
  FastAPI codebases hide more blocking ORM calls than most, which is
  where this migration hurts. Port the drivers first (asyncpg), push
  the rest to `asyncio.to_thread` (guide ch15).
- Advanced Pydantic types (nested unions, custom validators) map to plain
  `Schema` fields plus handler-level checks. Port the models first; they
  carry the most risk.
- Startup/shutdown events → `on_startup` / `on_shutdown` + lifespan.
- `request.url_for` → `router.url_for(name, **params)` (names from handler
  or `Blueprint` namespace).

## Plan

1. Port `Schema` models; assert 400s match (`extra="forbid"` where the
   FastAPI model rejected unknowns).
2. Move routes one file at a time; keep response bodies byte-identical and
   diff them with both TestClients.
3. Move auth last (tokens stay valid — same HS256 shape if you keep the secret).
4. Gate deploys on `ikarem check myapp:app`.

## Landing snippet (executed in CI)

```python
from ikarem import Depends, Ikarem, Schema
from ikarem.testing import TestClient


class Item(Schema):
    name: str
    qty: int = 1


def get_prefix():
    return "hi"


app = Ikarem(enable_docs=False)


@app.post("/items")
async def create(item: Item, prefix=Depends(get_prefix)):
    return {"msg": f"{prefix} {item.name}", "qty": item.qty}


c = TestClient(app)
assert c.post("/items", body={"name": "apple", "qty": "2"}).json() == {"msg": "hi apple", "qty": 2}
assert c.post("/items", body={"qty": 1}).status_code == 400
```
