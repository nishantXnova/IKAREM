import asyncio

from ikarem import (
    BackgroundTasks,
    Depends,
    Ikarem,
    MemoryCache,
    Schema,
    cached,
    check_password,
    create_token,
    hash_password,
)
from ikarem.security import CORSMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from ikarem.testing import TestClient


def test_validation_coercion():
    class M(Schema):
        name: str
        age: int = 0

    m = M.validate({"name": "a", "age": "3"})
    assert m.age == 3
    try:
        M.validate({"age": 1})
        assert False
    except Exception:
        pass


def test_di_and_schema_body():
    app = Ikarem(enable_docs=False)

    def get_prefix():
        return "hi"

    class Item(Schema):
        name: str
        qty: int = 1

    @app.post("/items")
    async def create(req, item: Item, prefix=Depends(get_prefix)):
        return {"msg": f"{prefix} {item.name}", "qty": item.qty}

    r = TestClient(app).post("/items", body={"name": "apple", "qty": "2"})
    assert r.status_code == 200, r.text
    assert r.json() == {"msg": "hi apple", "qty": 2}


def test_di_validation_error_is_400():
    app = Ikarem(enable_docs=False)

    class Item(Schema):
        name: str

    @app.post("/x")
    async def h(req, item: Item):
        return {"ok": True}

    assert TestClient(app).post("/x", body={}).status_code == 400


def test_jwt_and_rbac():
    secret = "s3cret"
    admin_tok = create_token("u1", secret, roles=["admin"])
    user_tok = create_token("u2", secret, roles=["user"])
    app = Ikarem(enable_docs=False, auth_secret=secret)
    from ikarem import require_roles

    @app.get("/admin")
    async def adm(req, claims=Depends(require_roles("admin"))):
        return {"sub": claims["sub"]}

    c = TestClient(app)
    assert c.get("/admin", headers={"authorization": f"Bearer {admin_tok}"}).status_code == 200
    assert c.get("/admin", headers={"authorization": f"Bearer {user_tok}"}).status_code == 403
    assert c.get("/admin").status_code == 401


def test_passwords():
    h = hash_password("pw123")
    assert check_password("pw123", h) and not check_password("no", h)


def test_passwords_legacy_rounds_still_verify():
    # Pre-600k hashes keep working (re-hash on next login rotates them).
    import hashlib

    salt = "oldsalt1234567890"
    dk = hashlib.pbkdf2_hmac("sha256", b"pw123", salt.encode(), 210_000)
    legacy = f"pbkdf2${salt}${dk.hex()}"
    assert check_password("pw123", legacy) and not check_password("no", legacy)


def test_cors_and_security_and_ratelimit():
    app = Ikarem(enable_docs=False)
    app.use(CORSMiddleware())
    app.use(SecurityHeadersMiddleware())
    app.use(RateLimitMiddleware(per_minute=2))

    @app.get("/a")
    async def a(req):
        return "ok"

    c = TestClient(app)
    r = c.get("/a")
    assert r.headers.get("access-control-allow-origin") == "*"
    assert r.headers.get("x-frame-options") == "DENY"
    c.get("/a")
    assert c.get("/a").status_code == 429


def test_background_tasks_run():
    app = Ikarem(enable_docs=False)
    done = []

    @app.post("/job")
    async def job(req, bg: BackgroundTasks):
        bg.add(done.append, "ran")
        return {"ok": True}

    assert TestClient(app).post("/job", body={}).status_code == 200
    assert done == ["ran"]


def test_cache_and_cached_decorator():
    async def _go():
        cache = MemoryCache()
        calls = []

        @cached(cache, ttl=60)
        async def expensive(x):
            calls.append(x)
            return x * 2

        assert await expensive(2) == 4
        assert await expensive(2) == 4
        assert calls == [2]

    asyncio.run(_go())


def test_system_and_docs_routes():
    app = Ikarem()
    c = TestClient(app)

    @app.get("/ping")
    async def ping(req):
        """Ping check."""
        return {"pong": True}

    assert c.get("/healthz").json()["status"] == "ok"
    assert "ikarem_requests" in c.get("/metrics").text
    spec = c.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.1")
    assert "/ping" in spec["paths"]
    assert c.get("/docs").status_code == 200
