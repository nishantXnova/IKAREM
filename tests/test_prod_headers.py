"""Production response contract: every response type carries middleware headers,
file responses support cookies, and errors keep request-ID/security headers."""

from ikarem import Ikarem, SessionMiddleware
from ikarem.observability import RequestIDMiddleware
from ikarem.security import SecurityHeadersMiddleware
from ikarem.static import FileResponse
from ikarem.testing import TestClient


def _app():
    app = Ikarem(enable_docs=False, session_secret="s")
    app.use(RequestIDMiddleware())
    app.use(SecurityHeadersMiddleware())
    app.use(SessionMiddleware())

    @app.get("/f")
    async def f(req):
        req.session["touched"] = True
        return FileResponse(__file__, filename="t.py")

    @app.get("/boom")
    async def boom(req):
        raise RuntimeError("kaboom")

    return app


def test_file_response_carries_middleware_headers_and_cookies():
    r = TestClient(_app()).get("/f")
    assert r.status_code == 200, r.text
    assert r.headers.get("x-request-id")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert "ikarem_session" in r.cookies  # session saved onto a file response
    assert "attachment" in r.headers.get("content-disposition", "")


def test_error_responses_keep_middleware_headers():
    c = TestClient(_app())
    r404 = c.get("/nope")
    assert r404.status_code == 404
    assert r404.headers.get("x-request-id")
    assert r404.headers.get("x-content-type-options") == "nosniff"
    r500 = c.get("/boom")
    assert r500.status_code == 500
    assert r500.headers.get("x-request-id")
