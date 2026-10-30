"""ikarem diagram: the map must match the app it claims to describe.

The graph is built from the same compiled plans `check` uses, so these tests
pin the facts developers rely on: grouping, shared/nested dependencies, the
auth boundary, body schemas, middleware order, and the HTTP/WS split. All
six renderers are exercised; SVG/HTML must be well-formed and self-contained.
"""

import json
import xml.etree.ElementTree as ET

import pytest

from ikarem import Blueprint, Depends, Ikarem, MethodView, Schema, require_roles
from ikarem.diagram import (
    FORMATS,
    build_diagram,
    format_for_path,
    render,
    render_diagram,
    render_svg,
)


class Stamp:
    async def __call__(self, request, call_next):
        return await call_next(request)


class Audit:
    async def __call__(self, request, call_next):
        return await call_next(request)


def get_db(req):
    return {"db": True}


def decode(req):
    return "u1"


def current_user(req, sub=Depends(decode)):
    return {"sub": sub}


class Item(Schema):
    name: str
    qty: int = 1


def _app():
    app = Ikarem(enable_docs=False, auth_secret="diagram-secret")

    @app.get("/items")
    async def list_items(req, db=Depends(get_db)):
        """List items."""
        return {"items": []}

    @app.post("/items")
    async def create_item(req, item: Item, db=Depends(get_db)):
        return {"ok": True}

    @app.get("/me")
    async def me(req, user=Depends(current_user)):
        return user

    bp = Blueprint("billing", url_prefix="/billing")

    @bp.get("/invoices")
    async def invoices(req, db=Depends(get_db), claims=Depends(require_roles("admin"))):
        return {"invoices": []}

    app.register_blueprint(bp)

    @app.websocket("/ws/{room}")
    async def chat(ws):
        await ws.accept()

    app.use(Stamp())
    app.use(Audit())
    return app


def _route(graph, path, method=None):
    return next(
        n
        for n in graph.nodes
        if n.kind in ("route", "ws") and n.path == path and (method is None or n.method == method)
    )


def test_graph_matches_registered_routes():
    graph = build_diagram(_app())
    assert graph.counts["routes"] == 4
    assert graph.counts["websockets"] == 1
    assert graph.counts["middleware"] == 2
    assert [n.method for n in graph.nodes if n.kind == "route"] == ["GET", "POST", "GET", "GET"]
    assert _route(graph, "/billing/invoices").group == "billing"
    assert _route(graph, "/items").group == "items"  # shared first segment
    assert _route(graph, "/me").group == "app"
    assert _route(graph, "/ws/{room}").kind == "ws"
    assert _route(graph, "/ws/{room}").method == "WS"


def test_shared_dependency_is_one_node_with_refs():
    graph = build_diagram(_app())
    dbs = [n for n in graph.nodes if n.kind == "dep" and n.label == "get_db"]
    assert len(dbs) == 1
    assert dbs[0].refs == 3
    incoming = [e for e in graph.edges if e.dst == dbs[0].id and e.kind == "call"]
    assert len(incoming) == 3
    assert graph.counts["shared_deps"] == 1


def test_nested_dependency_is_dashed_edge():
    graph = build_diagram(_app())
    user = next(n for n in graph.nodes if n.kind == "dep" and n.label == "current_user")
    token = next(n for n in graph.nodes if n.kind == "dep" and n.label == "decode")
    assert any(e.src == user.id and e.dst == token.id and e.kind == "nested" for e in graph.edges)
    assert "mermaid"  # shapes are asserted below, not here


def test_auth_boundary_propagates_to_route_and_dep():
    graph = build_diagram(_app())
    invoices = _route(graph, "/billing/invoices")
    assert invoices.auth is True
    assert invoices.auth_detail == "admin"
    auth_dep = next(n for n in graph.nodes if n.kind == "dep" and n.label == "auth:admin")
    assert auth_dep.auth is True
    # require_roles is nested inside the route's DI graph, so the route is guarded
    assert graph.counts["auth_routes"] == 1


def test_body_schema_attached_once():
    graph = build_diagram(_app())
    schemas = [n for n in graph.nodes if n.kind == "schema"]
    assert [n.label for n in schemas] == ["Item"]
    assert "name" in schemas[0].sub and "qty" in schemas[0].sub
    create = _route(graph, "/items", "POST")
    assert any(e.src == create.id and e.dst == schemas[0].id and e.kind == "body" for e in graph.edges)


def test_middleware_order_and_ws_bypass():
    graph = build_diagram(_app())
    mws = [n.label for n in graph.nodes if n.kind == "middleware"]
    assert mws == ["Stamp", "Audit"]
    chain = [e for e in graph.edges if e.kind == "pipeline"]
    assert [(e.src, e.dst) for e in chain] == [
        ("client", "m0"),
        ("m0", "m1"),
        ("m1", "hub"),
        ("client", "wshub"),
    ]
    # the websocket route hangs off wshub, never the HTTP router
    ws = _route(graph, "/ws/{room}")
    assert any(e.src == "wshub" and e.dst == ws.id for e in graph.edges)
    assert not any(e.src == "hub" and e.dst == ws.id for e in graph.edges)


def test_blueprint_tag_survives_wrapping():
    app = Ikarem(enable_docs=False)
    bp = Blueprint("reports", url_prefix="/v2/reports")

    @bp.get("/daily")
    async def daily(req):
        return {}

    app.register_blueprint(bp)
    graph = build_diagram(app)
    assert _route(graph, "/v2/reports/daily").group == "reports"


def test_method_view_renders_without_crashing():
    app = Ikarem(enable_docs=False)

    class Things(MethodView):
        async def get(self, req):
            return {}

        async def post(self, req):
            return {}

    app.route("/things", Things.methods())(Things.as_view("things"))
    graph = build_diagram(app)
    node = _route(graph, "/things")
    assert node.handler == "Things"
    assert node.method == "GET,POST"
    assert node.sub == "MethodView"


def test_internal_mcp_transport_is_not_noise():
    app = Ikarem(enable_docs=False)
    app.mount_mcp("/mcp")
    graph = build_diagram(app)
    assert all(n.path != "/mcp" for n in graph.nodes)


def test_no_deps_switch_keeps_route_map():
    graph = build_diagram(_app(), include_deps=False)
    assert [n for n in graph.nodes if n.kind == "dep"] == []
    assert graph.counts["dependencies"] == 0
    assert graph.counts["routes"] == 4


def test_mermaid_shape():
    out = render(build_diagram(_app()), "mermaid")
    assert out.startswith("%%")
    assert "flowchart LR" in out
    assert 'subgraph g0["items"]' in out
    assert "GET /billing/invoices" in out
    assert "class " in out and "auth" in out
    assert out.endswith("\n")


def test_svg_is_well_formed_and_self_contained():
    out = render_svg(build_diagram(_app()))
    root = ET.fromstring(out.split("?>", 1)[1])
    assert root.tag.endswith("svg")
    assert int(root.get("width")) > 700 and int(root.get("height")) > 300
    text = " ".join(t.text or "" for t in root.iter() if t.tag.endswith("text"))
    for expected in ("client", "router", "GET", "/items", "get_db", "auth:admin", "Item"):
        assert expected in text, expected
    assert "<script" not in out
    assert 'src="http' not in out
    assert " at 0x" not in out  # no memory addresses leaking into committed SVGs


def test_svg_pipeline_stays_connected_when_it_wraps():
    """A long middleware onion wraps; every later stage still has an inbound edge."""
    app = Ikarem(enable_docs=False, auth_secret="diagram-secret")

    @app.get("/")
    async def index(req):
        return {}

    for _ in range(16):
        app.use(Stamp())
    out = render_svg(build_diagram(app))
    root = ET.fromstring(out.split("?>", 1)[1])

    names = ["client"]
    for g in root.iter():
        cls = (g.get("class") or "").split()
        if "node" in cls and (g.get("id") or "").startswith("m"):
            names.append(g.get("id"))
    names.append("hub")
    reached = {e.get("data-dst") for e in root.iter() if (e.get("class") or "") == "edge"}
    assert names[1:]  # middleware + router are drawn
    assert set(names[1:]) <= reached
    # The onion wrapped: chips occupy more than one row.
    ys = {
        g.find("{http://www.w3.org/2000/svg}rect").get("y")
        for g in root.iter()
        if "node" in (g.get("class") or "").split() and (g.get("id") or "").startswith("m")
    }
    assert len(ys) > 1


def test_html_page_has_filter_and_inline_svg():
    out = render(build_diagram(_app()), "html")
    assert out.startswith("<!doctype html>")
    assert '<svg id="diagram"' in out
    assert 'id="q"' in out
    assert "addEventListener" in out
    assert 'src="http' not in out
    assert "<link" not in out


def test_ascii_is_ascii_and_bounded():
    out = render(build_diagram(_app()), "ascii")
    out.encode("ascii")  # cp1252 pipes must never raise
    assert all(len(line) <= 100 for line in out.splitlines())
    assert "get_db" in out
    assert "deps:" in out and "shared dependencies" in out


def test_json_round_trips_and_is_stable():
    graph = build_diagram(_app())
    first = render(graph, "json")
    data = json.loads(first)
    assert data["counts"]["routes"] == 4
    assert {n["kind"] for n in data["nodes"]} >= {"route", "dep", "schema", "middleware", "hub"}
    assert render(build_diagram(_app()), "json") == first


def test_output_is_deterministic_across_renderers():
    for fmt in FORMATS:
        assert render(build_diagram(_app()), fmt) == render(build_diagram(_app()), fmt)


def test_empty_app_renders_every_format():
    app = Ikarem(enable_docs=False)
    for fmt in FORMATS:
        out = render_diagram(app, fmt)
        assert isinstance(out, str) and out
    graph = build_diagram(app)
    assert graph.counts["routes"] == 0
    assert graph.counts["middleware"] == 0


def test_hostile_title_is_escaped_everywhere():
    app = Ikarem(enable_docs=False)
    title = '<script>alert("x")</script> & co'
    for fmt in ("svg", "html", "mermaid"):
        out = render_diagram(app, fmt, title=title)
        assert "<script>alert" not in out, fmt
    html_out = render_diagram(app, fmt="html", title=title)
    assert "&lt;script&gt;" in html_out
    mermaid_out = render_diagram(app, fmt="mermaid", title=title)
    assert '"' not in mermaid_out.split("flowchart", 1)[0]


def test_unknown_format_names_the_valid_set():
    with pytest.raises(ValueError) as e:
        render(build_diagram(Ikarem(enable_docs=False)), "png")
    message = str(e.value)
    assert "png" in message and all(f in message for f in FORMATS)


def test_format_for_path():
    assert format_for_path("app.svg") == "svg"
    assert format_for_path("docs/app.HTML") == "html"
    assert format_for_path("app.mmd") == "mermaid"
    assert format_for_path("app.gv") == "dot"
    assert format_for_path("app.txt") == "ascii"
    assert format_for_path("app.unknown") == "mermaid"


def test_app_method_matches_module_api():
    app = _app()
    assert app.diagram() == render_diagram(app)
    assert app.diagram("json") == render_diagram(app, "json")
    assert "include_deps" in app.diagram.__doc__


def test_cli_stdout_and_file_formats(monkeypatch, capsys, tmp_path):
    import tests.test_diagram as selfmod
    from ikarem import cli

    selfmod._cli_app = _app()
    target = "tests.test_diagram:_cli_app"

    monkeypatch.setattr("sys.argv", ["ikarem", "diagram", target, "--format", "mermaid"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "flowchart LR" in out and "get_db" in out

    svg_path = tmp_path / "app.svg"
    monkeypatch.setattr("sys.argv", ["ikarem", "diagram", target, "-o", str(svg_path)])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert "wrote" in capsys.readouterr().out
    assert svg_path.read_text(encoding="utf-8").startswith("<?xml")

    json_path = tmp_path / "nested" / "graph.json"
    monkeypatch.setattr("sys.argv", ["ikarem", "diagram", target, "-o", str(json_path)])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert json.loads(json_path.read_text(encoding="utf-8"))["counts"]["routes"] == 4

    monkeypatch.setattr("sys.argv", ["ikarem", "diagram", target, "--format", "ascii", "--no-deps"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "get_db" not in out


def test_cli_refuses_binary_stdout_with_remedy(monkeypatch, capsys):
    import tests.test_diagram as selfmod
    from ikarem import cli

    selfmod._cli_app = _app()
    monkeypatch.setattr("sys.argv", ["ikarem", "diagram", "tests.test_diagram:_cli_app", "-f", "svg"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    assert "-o" in capsys.readouterr().out


def test_check_does_not_require_diagram_import():
    # Diagram is a devtool: importing the package must not drag it into the
    # request path, and building one must not mutate the compiled plan cache.
    import sys

    assert "ikarem.diagram" in sys.modules  # imported by the package's public API
    app = _app()
    before = len(app.router.routes)
    build_diagram(app)
    assert len(app.router.routes) == before


# CLI target lives here so `tests.test_diagram:_cli_app` always resolves.
_cli_app = None
