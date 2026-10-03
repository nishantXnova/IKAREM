# IKAREM ecosystem registry

Flask has extensions. FastAPI has contrib. Django has packages. This is
ours — small on purpose. One naming rule, one interface checklist, one
table. No registry server, no approval queue: a PR adding a row.

## Naming

- Package: `ikarem-<name>` (e.g. `ikarem-admin`). Import: `ikarem_<name>`.
- Optional drivers stay extras: `pip install ikarem-<name>[postgres]`.
  Core stays stdlib-only — an extension that hard-requires a driver in
  `import ikarem_x` is rejected (lazy import inside methods, error names
  the exact extra, e.g. `pip install ikarem-admin[postgres]`).

## Skeleton (the whole contract)

A plugin is a plain object. `register` wires routes/config immediately;
lifecycle hooks are optional. Dependencies topo-sort, cycles fail loudly.

```python
from ikarem import Ikarem
from ikarem.plugins import BasePlugin


class Hello(BasePlugin):
    name = "hello"
    requires = []
    priority = 50

    def register(self, app):
        @app.get("/hello")
        async def hello(req):
            return {"hello": "ikarem"}


app = Ikarem(enable_docs=False)
app.register(Hello())
```

## Submission checklist (PR adds one row below)

1. Name follows `ikarem-<name>`; install is one `pip install` line.
2. States its interface: plugin (`name/requires/priority/register` +
   hooks) and/or `CacheBackend` (`get/set/delete`, `None` = miss) and/or
   `DatabaseConnector` (`?` placeholders, `dict` rows, lazy driver,
   loop-safe pools — see `docs/PLUGINS.md`).
3. Hermetic tests (tmp dirs, unique names, no shared state); failures
   name the remedy, never swallow silently.
4. No change to `ikarem/` core, no new required dependency.

## Shipped (in-core proofs of each interface)

| Extension | Interface | Lives in |
|---|---|---|
| DatabasePlugin (sqlite/postgres/mysql/sqlserver) | `DatabaseConnector` + plugin lifecycle | `ikarem/db/` |
| QueuePlugin + `@task` + `ikarem worker` | plugin + portable leases | `ikarem/queue.py` |
| SchedulerPlugin + `app.cron` / `app.every` | plugin + lifespan | `ikarem/scheduler.py` |
| `MemoryCache` / `RedisCache` + `@cached` | `CacheBackend` | `ikarem/cache.py` |
| NISH responses (`to_nish`, `NISHResponse`, `negotiate`) | response format | `ikarem/nish.py` + `docs/NISH.md` |
| CORS / security headers / trusted hosts / rate limit | middleware | `ikarem/security.py` |
| Timeouts / bulkheads / idempotency | middleware | `ikarem/resilience.py` |
| Meraki compat, Flask takes (blueprints, templates, flash, MethodView) | shims | `ikarem/meraki_compat.py`, `ikarem/blueprints.py` |

## Wanted (claim one, follow the checklist)

| Extension | Interface | Notes |
|---|---|---|
| `ikarem-admin` | plugin over `app.resource()` | Browsable CRUD; owner-scoping stays spoof-proof |
| `ikarem-oauth` | auth Depends | Refresh tokens + OAuth2 code flow; current JWT is bare HS256 |
| `ikarem-s3` | storage backend | Presigned uploads; local-filesystem fallback for tests |
| `ikarem-mail` | plugin + queue task | SMTP lazy extra; welcome-mail is the reference job |
| `ikarem-otel` exporter pack | middleware | `TracingMiddleware` is core (spans + propagation, `ikarem[otel]`); the extension ships Collector/exporter presets |

## Third-party

| Extension | Interface | Maintainer |
|---|---|---|
| _yours here_ | — | — |

First stranger to ship a row gets the design review free.
