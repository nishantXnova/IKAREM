"""Exhaustive rate-limit verification: 7 branches."""

import asyncio

from ikarem import Ikarem
from ikarem.security import RateLimitMiddleware
from ikarem.testing import TestClient


def _app(limit, clock=None):
    app = Ikarem(enable_docs=False)
    app.use(RateLimitMiddleware(per_minute=limit, clock=clock))
    app._rl = app.middleware.stack[-1]

    @app.get("/r")
    async def r(req):
        return {"ok": True}

    return app


def test_1_limit_allows_under_threshold():
    c = TestClient(_app(3))
    for _ in range(3):
        assert c.get("/r").status_code == 200


def test_2_window_resets_after_60s():
    now = [1000.0]
    app = _app(2, clock=lambda: now[0])
    c = TestClient(app)
    assert c.get("/r").status_code == 200
    assert c.get("/r").status_code == 200
    assert c.get("/r").status_code == 429
    now[0] += 61  # next window
    assert c.get("/r").status_code == 200


def test_3_429_with_retry_after():
    c = TestClient(_app(1))
    assert c.get("/r").status_code == 200
    r = c.get("/r")
    assert r.status_code == 429
    assert r.json() == {"detail": "rate limit exceeded"}
    assert "retry-after" in r.headers
    assert int(r.headers["retry-after"]) >= 1


def test_4_retry_after_counts_down():
    now = [500.0]
    app = _app(1, clock=lambda: now[0])
    c = TestClient(app)
    c.get("/r")
    first = int(c.get("/r").headers["retry-after"])
    now[0] += 30
    second = int(c.get("/r").headers["retry-after"])
    assert second < first, (first, second)


def test_5_headers_on_all_responses():
    c = TestClient(_app(2))
    ok = c.get("/r")
    assert ok.headers["x-ratelimit-limit"] == "2"
    assert ok.headers["x-ratelimit-remaining"] == "1"
    assert "x-ratelimit-reset" in ok.headers
    blocked = c.get("/r")  # 2nd ok
    assert blocked.headers["x-ratelimit-remaining"] == "0"
    over = c.get("/r")  # 429 still carries headers
    assert over.status_code == 429
    assert over.headers["x-ratelimit-limit"] == "2"
    assert over.headers["x-ratelimit-remaining"] == "0"


def test_6_independent_ips():
    app = _app(1)
    c = TestClient(app)
    assert c.get("/r", headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
    assert c.get("/r", headers={"x-forwarded-for": "1.1.1.1"}).status_code == 429
    assert c.get("/r", headers={"x-forwarded-for": "2.2.2.2"}).status_code == 200


def test_7_concurrent_requests_share_budget():
    app = _app(3)
    c = TestClient(app)

    async def _go():
        results = await asyncio.gather(*[c._do("GET", "/r", None, {}, "") for _ in range(10)])
        return [r.status_code for r in results]

    codes = asyncio.run(_go())
    assert codes.count(200) == 3, codes
    assert codes.count(429) == 7, codes
