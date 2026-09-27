# Extending IKAREM: plugins, middleware, connectors, and friends

Everything below is a plain protocol — no registration magic, no metaclasses.
If it walks like the interface, the framework picks it up.

## Middleware

```python
async def timing(req, call_next):
    resp = await call_next(req)  # downstream; omit to short-circuit
    resp.headers["x-took"] = "..."
    return resp  # must return a Response (or Response-like)


app.use(timing)
```

Contract: `(request, call_next) -> response`. Return without calling
`call_next` to short-circuit (auth gates, rate limits). The returned object
needs `.status_code`, `.headers` (dict), and an ASGI `__call__` —
`FileResponse` qualifies; anything else goes through `to_response`.
Optional hook checked at startup: `validate_config(app)` raising
`RuntimeError` with a fix — fail before traffic, not on first request.

## Plugins

```python
class Audit(BasePlugin):  # or plain object with these attrs
    name = "audit"
    requires = ["database"]  # topo-sorted; cycles rejected at startup
    priority = 50  # tie-break, lower first

    def register(self, app): ...  # wire routes/config immediately
    async def on_startup(self, app): ...
    async def on_shutdown(self, app): ...
    async def on_request(self, req): ...
    async def on_response(self, req, resp): ...
```

Register with `app.register(Audit())`. Startup failures abort boot loudly —
plugins must never swallow their own errors.

## Database connectors

Subclass `DatabaseConnector`, set `dialect`, implement
`connect/disconnect/execute/fetch_one/fetch_all/transaction`. Rules:

- Placeholders are always `?` in user SQL — normalize per engine.
- `fetch_*` return plain `dict`s (JSON-serializable, no driver types).
- Import the driver lazily inside methods; missing driver raises
  `RuntimeError("pip install ikarem[<extra>] ...")`.
- Pools are loop-bound: track the creating loop and rebuild on change
  (see `PostgresConnector._pool_for`), terminate (don't close) stale pools.
- Single shared connections (SQLite style) serialize use inside worker
  threads with a `threading.Lock` — never `asyncio.Lock` (binds one loop).

## Cache backends

Implement `CacheBackend` (`get/set/delete`, `set` takes `ttl=`). `None`
means miss — never store bare `None` as a value.

## Auth dependencies

Any `Depends()` callable returning claims (or raising 401/403) is an auth
boundary. Tag it so OpenAPI/MCP advertise it:

```python
async def _check(request): ...


_check._ikarem_security = {"scheme": "bearer", "roles": ("admin",)}
```

Set `_ikarem_config_secret = True` when the secret comes from app config —
startup then refuses to boot with default secrets instead of minting
forgable tokens.

## Scheduler jobs & queue tasks

- Cron/interval: `@app.cron("*/5 * * * *")` / `@app.every(30)`; start
  explicitly with `await app.start_scheduler(stop)`. Jobs must be quick —
  heavy work goes to the queue. Exceptions are counted (`job.errors`) and
  logged, never fatal to the loop.
- Queue: `@task("name")` registers `fn(**args)` globally;
  `QueuePlugin` (after `DatabasePlugin`) exposes `app.state_queue`;
  `ikarem worker` drains it. Payloads must be JSON-serializable.
