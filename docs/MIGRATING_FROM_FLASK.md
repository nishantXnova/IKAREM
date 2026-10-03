# Migrating from Flask

Estimated cost: a day (no production migrations behind this yet). Half the API already crossed over: `Blueprint`,
`MethodView`, `abort`, and `flash` exist in IKAREM with the same names
(see "Flask's best, taken" in the README). The real work is globals →
explicit `req`, and sync → async.

## Map

| Flask | IKAREM |
|---|---|
| `@app.route("/x", methods=["POST"])` | `@app.post("/x")` (get/post/put/patch/delete) |
| Global `request` proxy | Explicit `req` handler param (no magic) |
| `request.args` / `request.form` / `request.files` | `req.query` / `await req.form()` (`UploadFile`) |
| `jsonify(...)` | `return {...}` |
| `render_template(...)` | `Templates(dir).response(...)` (`ikarem[jinja]`) |
| `flash(...)` | `flash(...)` (same name, session-backed) |
| `Blueprint` | `Blueprint` (same name, own hooks + errors) |
| `MethodView` | `MethodView` (same name, full DI per method) |
| `abort(403)` | `abort(403, detail)` (same name, pipeline-safe) |
| `g` request globals | `req.state` / handler params |
| `before_request` / `after_request` | Middleware (`app.use`) or blueprint hooks |
| `flask test_client` | `TestClient` (cookie jar included) |
| gunicorn + sync workers | Any ASGI server (`app.run()` needs `ikarem[server]`) |

## Deltas that bite

- Everything is async. Blocking calls (psycopg2, `time.sleep`, CPU loops)
  must move to async drivers or threads — this is the port, not the syntax.
- No app context / current_app: configuration arrives via `app.config` and
  `IKAREM_*` env vars, reads are explicit.
- Sessions are signed cookies with CSRF on unsafe routes by default; Flask
  sessions "just worked" — here the first unsafe POST teaches you the token.
- Extensions (`flask-sqlalchemy`, `flask-login`) have no drop-ins: the
  `DatabaseConnector` strategy and JWT/sessions are the replacements.

## Plan

1. Routes first, keeping `jsonify` shapes as plain dicts; diff responses.
2. Replace `request` global uses with `req` params handler by handler.
3. Convert blocking I/O (DB driver first — biggest stall risk).
4. Sessions + CSRF + auth, then `ikarem check myapp:app`.

## Landing snippet (executed in CI)

```python
from ikarem import Blueprint, Ikarem, abort
from ikarem.testing import TestClient

api = Blueprint("api", url_prefix="/api")
app = Ikarem(enable_docs=False)


@api.get("/items/{uid:int}")
async def one(req, uid: int):
    if uid == 0:
        abort(403, "owner only")
    return {"uid": uid}


app.register_blueprint(api)

c = TestClient(app)
assert c.get("/api/items/3").json() == {"uid": 3}
assert c.get("/api/items/0").status_code == 403
```
