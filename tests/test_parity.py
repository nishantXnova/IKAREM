"""Parity batch: OpenAPI apiKey, RedisCache (fake client), SchedulerPlugin lifespan."""

import asyncio

import pytest

from ikarem import APIKeyAuth, Depends, Ikarem, require_scopes
from ikarem.cache import MemoryCache, RedisCache
from ikarem.openapi import build_openapi
from ikarem.scheduler import SchedulerPlugin


class _FakeRedis:
    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def setex(self, k, ttl, v):
        self.d[k] = v.encode() if isinstance(v, str) else v

    async def delete(self, k):
        self.d.pop(k, None)


def _app_both():
    app = Ikarem(enable_docs=False, auth_secret="s3cret-parity")
    bearer = require_scopes("read")

    @app.get("/bearer")
    async def bearer_h(req, claims=Depends(bearer)):
        return {"ok": True}

    api = APIKeyAuth({"svc": {"name": "svc"}}, header="x-api-key")

    @app.get("/keyed")
    async def keyed(req, info=Depends(api)):
        return {"ok": True}

    return app


def test_openapi_emits_bearer_and_apikey_schemes():
    spec = build_openapi(_app_both())
    assert spec["paths"]["/bearer"]["get"]["security"] == [{"bearerAuth": []}]
    assert spec["paths"]["/keyed"]["get"]["security"] == [{"apiKeyAuth": []}]
    schemes = spec["components"]["securitySchemes"]
    assert schemes["bearerAuth"] == {"type": "http", "scheme": "bearer"}
    assert schemes["apiKeyAuth"] == {"type": "apiKey", "in": "header", "name": "x-api-key"}


def test_openapi_custom_apikey_header_name():
    app = Ikarem(enable_docs=False)
    api = APIKeyAuth({"a": {"n": 1}}, header="x-custom-key")

    @app.get("/c")
    async def c(req, info=Depends(api)):
        return {"ok": True}

    spec = build_openapi(app)
    assert spec["components"]["securitySchemes"]["apiKeyAuth"]["name"] == "x-custom-key"


def test_manifest_carries_scheme_header_scopes():
    app = _app_both()
    manifest = app.check()["routes"]
    keyed = [r for r in manifest if r["path"] == "/keyed"][0]
    assert keyed["handler"] == "keyed"
    # describe_app path: via compiled plan
    from ikarem.compiled import describe_app

    desc = {(e["method"], e["path"]): e for e in describe_app(app)["routes"]}
    assert desc[("GET", "/keyed")]["auth"]["scheme"] == "apiKey"
    assert desc[("GET", "/keyed")]["auth"]["header"] == "x-api-key"
    assert desc[("GET", "/bearer")]["auth"]["scheme"] == "bearer"


def test_mcp_description_names_apikey_header():
    app = Ikarem(enable_docs=False)
    api = APIKeyAuth({"a": 1})

    @app.get("/k")
    async def k(req, info=Depends(api)):
        """Keyed op."""
        return {"ok": True}

    tools = {t["name"]: t for t in app.mcp_tools()}
    assert "x-api-key" in tools["k"]["description"]
    assert "Bearer" not in tools["k"]["description"]


def test_redis_cache_roundtrip_with_fake_client():
    async def _go():
        c = RedisCache(prefix="t:", client=_FakeRedis())
        assert await c.get("missing") is None
        await c.set("k", {"status": 200, "body_b64": "e30="}, ttl=60)
        assert await c.get("k") == {"status": 200, "body_b64": "e30="}
        await c.delete("k")
        assert await c.get("k") is None

    asyncio.run(_go())


def test_redis_cache_rejects_non_json_with_remedy():
    async def _go():
        c = RedisCache(prefix="t:", client=_FakeRedis())
        with pytest.raises(ValueError, match="JSON-compatible"):
            await c.set("k", {"v": object()})

    asyncio.run(_go())


def test_redis_cache_missing_dep_names_extra():
    import sys

    saved = sys.modules.pop("redis", None)
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "redis" or name.startswith("redis."):
            raise ImportError("blocked")
        return real_import(name, *a, **k)

    builtins.__import__ = _blocked
    try:

        async def _go():
            c = RedisCache(prefix="t:")
            with pytest.raises(RuntimeError, match=r"pip install ikarem\[redis\]"):
                await c.get("k")

        asyncio.run(_go())
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["redis"] = saved


def test_scheduler_plugin_lifespan_starts_and_stops():
    async def _go():
        app = Ikarem(enable_docs=False)
        fired = []

        @app.every(1000)
        async def job():
            fired.append(1)

        app.register(SchedulerPlugin(poll=0.01))
        await app.startup()
        assert getattr(app, "state_scheduler", None) is not None
        # background loop ticks without manual start_scheduler
        await asyncio.sleep(0.05)
        await app.shutdown()
        # plugin cancelled its task; second shutdown is safe
        await app.shutdown()

    asyncio.run(_go())


def test_scheduler_plugin_autostart_false_and_bad_poll():
    with pytest.raises(ValueError, match="positive"):
        SchedulerPlugin(poll=0)

    async def _go():
        app = Ikarem(enable_docs=False)

        @app.every(1000)
        async def job():
            pass

        app.register(SchedulerPlugin(poll=0.01, autostart=False))
        await app.startup()
        assert getattr(app, "state_scheduler", None) is not None
        await app.shutdown()

    asyncio.run(_go())


def test_memory_cache_still_default_for_idempotency():
    from ikarem import IdempotencyMiddleware

    m = IdempotencyMiddleware()
    assert isinstance(m.cache, MemoryCache)
