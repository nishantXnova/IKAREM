"""Meraki apps run unmodified on IKAREM: their example + their assertions."""

from ikarem.meraki_compat import Meraki, MerakiRequest, MerakiResponse
from ikarem.testing import TestClient


def _example_app():
    # verbatim shape of meraki-dummy-ahh/Meraki/example.py
    from ikarem.meraki_compat import Meraki as M

    app = M()

    @app.get("/")
    async def home(request):
        return MerakiResponse(body=b"Hello from Meraki!")

    @app.post("/users")
    async def create_user(request):
        return MerakiResponse(body=b"User created", status_code=201)

    return app


def test_meraki_example_runs_on_ikarem():
    c = TestClient(_example_app())
    r = c.get("/")
    assert r.status_code == 200
    assert r.text == "Hello from Meraki!"
    assert r.headers["content-type"] == "text/plain"
    assert c.post("/users").status_code == 201


def test_meraki_404_405_bodies_preserved():
    c = TestClient(_example_app())
    assert c.get("/missing").status_code == 404
    assert c.get("/missing").text == "Not Found"
    assert c.post("/").status_code == 405
    assert c.post("/").text == "Method Not Allowed"


def test_meraki_middleware_and_request_shape():
    seen = {}
    app = Meraki()

    async def mw(request, call_next):
        seen["headers_type"] = type(request.headers)
        seen["qp"] = request.query_params
        seen["method"] = request.method
        return await call_next(request)

    app.add_middleware(mw)

    @app.get("/q")
    async def q(request):
        assert isinstance(request, MerakiRequest)
        return MerakiResponse(body=b"ok")

    r = TestClient(app).get("/q", query="a=1&b=2")
    assert r.status_code == 200
    assert seen["headers_type"] is list
    assert ("a", "1") in seen["qp"] and ("b", "2") in seen["qp"]
    assert seen["method"] == "GET"


def test_escape_hatches_then_native_upgrades():
    app = Meraki()

    @app.post("/echo")
    async def echo(request):
        assert request.ikarem is not None  # full IKAREM request underneath
        return {"you_sent": await request.ikarem.json()}  # dicts just work

    @app.get("/users/{uid:int}")
    async def one(request):
        # Meraki can't do this at all — free upgrade on day one
        return {"uid": request.ikarem.path_params["uid"]}

    c = TestClient(app)
    assert c.post("/echo", body={"x": 1}).json() == {"you_sent": {"x": 1}}
    assert c.get("/users/7").json() == {"uid": 7}
    assert c.get("/users/nope").status_code == 404
