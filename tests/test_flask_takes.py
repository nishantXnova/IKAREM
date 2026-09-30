"""Flask takes: blueprints, templates, flash, MethodView, abort."""

import pytest

from ikarem import (
    Blueprint,
    Field,
    Ikarem,
    MethodView,
    Schema,
    SessionMiddleware,
    Templates,
    abort,
    flash,
    get_flashed_messages,
)
from ikarem.errors import Forbidden, NotFound
from ikarem.testing import TestClient


def test_blueprint_prefix_hooks_errors_namespacing():
    app = Ikarem(enable_docs=False)
    bp = Blueprint("admin", url_prefix="/admin")
    seen = []

    @bp.before_request
    async def gate(req):
        seen.append("before")
        if req.query.get("key") != "s3cret":
            return {"detail": "nope"}, 403

    @bp.after_request
    async def tag(req, resp):
        resp.headers["x-bp"] = "admin"
        return resp

    @bp.errorhandler(NotFound)
    async def nf(req, exc):
        return {"detail": "admin has no such thing"}

    @bp.get("/dash")
    async def dash(req):
        return {"ok": True}

    @bp.get("/gone")
    async def gone(req):
        abort(404)

    app.register_blueprint(bp)
    c = TestClient(app)
    assert c.get("/admin/dash").status_code == 403
    r = c.get("/admin/dash", query="key=s3cret")
    assert r.json() == {"ok": True} and r.headers["x-bp"] == "admin"
    assert seen == ["before", "before"]
    assert c.get("/admin/gone", query="key=s3cret").json() == {"detail": "admin has no such thing"}
    assert c.get("/nope").status_code == 404
    assert app.router.url_for("admin.dash") == "/admin/dash"


def test_blueprint_full_handler_contracts():
    """Regression: blueprint routes with typed params, DI, bodies, and
    background tasks must resolve exactly like app routes (single
    resolution, hooks wrapping, docs seeing the real contract)."""
    from ikarem import BackgroundTasks, Depends

    app = Ikarem(enable_docs=False)
    bp = Blueprint("shop", url_prefix="/shop")
    seen = []
    done = []

    @bp.before_request
    async def tag_in(req):
        seen.append("before")

    @bp.after_request
    async def tag_out(req, resp):
        resp.headers["x-bp"] = "shop"
        return resp

    def tax():
        return 2

    class Line(Schema):
        name: str
        qty: int = 1

    @bp.get("/items/{uid:int}")
    async def one(req, uid: int, verbose: int = 0, t=Depends(tax)):
        return {"uid": uid, "verbose": verbose, "t": t}

    @bp.post("/lines")
    async def add(req, line: Line, bg: BackgroundTasks):
        bg.add(done.append, line.name)
        return {"name": line.name, "qty": line.qty}, 201

    app.register_blueprint(bp)
    c = TestClient(app)
    r = c.get("/shop/items/7", query="verbose=1")
    assert r.json() == {"uid": 7, "verbose": 1, "t": 2}
    assert r.headers["x-bp"] == "shop"
    r = c.post("/shop/lines", body={"name": "apple", "qty": 3})
    assert r.status_code == 201 and done == ["apple"]
    assert c.post("/shop/lines", body={"qty": 1}).status_code == 400
    # hooks wrap handler invocation (the 400 fails in validation first)
    assert seen == ["before"] * 2
    assert app.router.url_for("shop.one", uid=7) == "/shop/items/7"
    from ikarem.openapi import build_openapi

    spec = build_openapi(app)
    assert spec["paths"]["/shop/items/{uid}"]["get"]["parameters"]
    assert "requestBody" in spec["paths"]["/shop/lines"]["post"]


def test_blueprint_prefix_override():
    app = Ikarem(enable_docs=False)
    bp = Blueprint("v", url_prefix="/v1")

    @bp.get("/ping")
    async def ping(req):
        return {"v": 1}

    app.register_blueprint(bp, url_prefix="/v2")
    assert TestClient(app).get("/v2/ping").json() == {"v": 1}


def test_templates_render_and_response(tmp_path):
    (tmp_path / "hi.html").write_text("<h1>hi {{ name }}!</h1>")
    t = Templates(tmp_path)
    assert t.render("hi.html", name="amy") == "<h1>hi amy!</h1>"
    # autoescape is on: user input can't break out
    assert "<script>" not in t.render("hi.html", name="<script>")
    r = t.response("hi.html", name="bob")
    assert r.status_code == 200 and b"bob" in r.body


def test_templates_missing_jinja_error(monkeypatch):
    import sys

    import ikarem.templating as tm

    monkeypatch.setitem(sys.modules, "jinja2", None)  # import jinja2 -> ImportError
    with pytest.raises(RuntimeError, match=r"ikarem\[jinja\]"):
        tm.Templates("/tmp").render("x.html")


def test_flash_roundtrip_and_template_safety():
    app = Ikarem(enable_docs=False, session_secret="s")
    app.use(SessionMiddleware())

    @app.post("/do")
    async def do(req):
        flash(req, "saved")
        flash(req, "careful", category="warn")
        return {"ok": True}

    @app.get("/show")
    async def show(req):
        return {"msgs": get_flashed_messages(req, with_categories=True)}

    c = TestClient(app)
    assert c.post("/do", body={}).status_code == 200
    assert c.get("/show").json() == {"msgs": [["info", "saved"], ["warn", "careful"]]}
    assert c.get("/show").json() == {"msgs": []}  # consumed: one-shot
    assert get_flashed_messages(object()) == []  # no session: safe [] for templates
    with pytest.raises(RuntimeError, match="SessionMiddleware"):
        flash(object(), "x")


def test_method_view_crud_with_di():
    from ikarem import Depends

    app = Ikarem(enable_docs=False)
    store = {}

    def get_store(req):
        return store

    class Items(MethodView):
        """Item collection."""

        async def get(self, req, db=Depends(get_store)):
            return {"items": sorted(db)}

        async def post(self, req, item: "ItemIn", db=Depends(get_store)):
            db[item.name] = item.qty
            return {"ok": True}, 201

    class ItemIn(Schema):
        name: str
        qty: int = Field(1, ge=1)

    # rebind annotation (defined after class for readability in test)
    Items.post.__annotations__["item"] = ItemIn
    assert Items.methods() == ["GET", "POST"]

    app.route("/items", ["GET", "POST"])(Items.as_view("items"))
    c = TestClient(app)
    assert c.get("/items").json() == {"items": []}
    assert c.post("/items", body={"name": "a", "qty": 2}).status_code == 201
    assert c.post("/items", body={"name": "b", "qty": 0}).status_code == 400
    assert c.get("/items").json() == {"items": ["a"]}


def test_method_view_405():
    app = Ikarem(enable_docs=False)

    class OnlyGet(MethodView):
        async def get(self, req):
            return {"ok": True}

    app.route("/g", ["GET", "POST"])(OnlyGet.as_view("g"))
    c = TestClient(app)
    assert c.get("/g").status_code == 200
    assert c.post("/g", body={}).status_code == 405


def test_abort_shapes():
    app = Ikarem(enable_docs=False)

    @app.get("/a")
    async def a(req):
        abort(403, "owner only")

    @app.get("/b")
    async def b(req):
        abort(404)

    @app.get("/c")
    async def teapot(req):
        abort(418, "teapot")

    @app.exception_handler(Forbidden)
    async def f(req, exc):
        return {"custom": True}, 403

    c = TestClient(app)
    assert c.get("/a").json() == {"custom": True}  # flows through handlers
    assert c.get("/b").json() == {"detail": "Not Found"}
    assert c.get("/c").status_code == 418
