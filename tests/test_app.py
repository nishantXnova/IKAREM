from ikarem import Ikarem
from ikarem.testing import TestClient


def make_app():
    app = Ikarem(debug=True)

    @app.get("/")
    async def home(req):
        return {"hello": "ikarem"}

    @app.get("/users/{uid:int}")
    async def get_user(req):
        return {"uid": req.path_params["uid"]}

    @app.post("/echo")
    async def echo(req):
        return await req.json()

    return app


def test_basic_route():
    c = TestClient(make_app())
    r = c.get("/")
    assert r.status_code == 200
    assert r.json() == {"hello": "ikarem"}


def test_path_converter():
    c = TestClient(make_app())
    assert c.get("/users/42").json() == {"uid": 42}
    assert c.get("/users/abc").status_code == 404  # int converter rejects


def test_405():
    c = TestClient(make_app())
    assert c.post("/", body={}).status_code == 405


def test_echo_json():
    c = TestClient(make_app())
    assert c.post("/echo", body={"a": 1}).json() == {"a": 1}
