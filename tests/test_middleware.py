from ikarem import Ikarem
from ikarem.middleware import Middleware
from ikarem.testing import TestClient


class HeaderMW(Middleware):
    async def __call__(self, req, call_next):
        resp = await call_next(req)
        resp.headers["x-ikarem"] = "crushed"
        return resp


class ShortCircuit(Middleware):
    async def __call__(self, req, call_next):
        from ikarem import TextResponse

        if req.path == "/blocked":
            return TextResponse("nope", status_code=403)
        return await call_next(req)


def test_middleware_after():
    app = Ikarem()
    app.use(HeaderMW())

    @app.get("/")
    async def h(req):
        return "ok"

    r = TestClient(app).get("/")
    assert r.headers.get("x-ikarem") == "crushed"


def test_middleware_short_circuit():
    app = Ikarem()
    app.use(ShortCircuit())

    @app.get("/blocked")
    async def h(req):
        return "never"

    r = TestClient(app).get("/blocked")
    assert r.status_code == 403
    assert r.text == "nope"
