"""Architecture diagrams from the compiled plans — stdlib only, no renderer.

One pass over ``app.router`` + ``app.ws_router`` + ``app.middleware`` builds a
small graph (routes, HTTP/WS pipeline, dependencies, body schemas), then any
of six renderers turns it into text you can commit or open:

    mermaid  GitHub/GitLab-native flowchart (default; diffs cleanly)
    svg      standalone file, hand-laid out (no Graphviz, no CDN)
    html     self-contained page: inline SVG, filter box, zoom buttons
    dot      Graphviz source when you want its layout engines
    ascii    terminal map, cp1252-safe
    json     the graph itself, for editors/agents

Facts the picture tells you that a route list does not: which handlers share
a dependency, where the auth boundary actually sits (auth propagates through
nested deps), which routes take a body schema, and that WebSocket routes
bypass the HTTP middleware onion.

Deterministic by construction: registration order in, byte-identical output
out — no timestamps, no addresses, no dict-hash ordering. Reflection happens
once per handler through ``compiled.get_plan`` (cached); rendering is pure
string work and never runs on the request path.

    ikarem diagram ledger.app:app -o docs/app.svg
    app.diagram("mermaid")
"""

from __future__ import annotations

import html
import inspect
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FORMATS = ("mermaid", "svg", "html", "dot", "ascii", "json")

# Extension -> format, for `-o app.svg` / `-o app.mmd`.
_EXT_FORMATS = {
    ".mmd": "mermaid",
    ".mermaid": "mermaid",
    ".md": "mermaid",
    ".svg": "svg",
    ".html": "html",
    ".htm": "html",
    ".dot": "dot",
    ".gv": "dot",
    ".txt": "ascii",
    ".json": "json",
}

_METHOD_COLORS = {
    "GET": "#16a34a",
    "POST": "#2563eb",
    "PUT": "#d97706",
    "PATCH": "#7c3aed",
    "DELETE": "#dc2626",
    "HEAD": "#64748b",
    "OPTIONS": "#64748b",
    "WS": "#7c3aed",
}
_METHOD_ORDER = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "WS")

_EDGE_COLORS = {
    "pipeline": "#f59e0b",
    "route": "#94a3b8",
    "call": "#64748b",
    "nested": "#a5b4fc",
    "body": "#10b981",
}


# ---- graph model ----


@dataclass
class Node:
    """One drawable box. ``kind`` picks the renderer style."""

    id: str
    label: str
    kind: str = "route"  # entry | middleware | hub | route | ws | dep | schema
    group: str = ""
    method: str = ""
    path: str = ""
    handler: str = ""
    detail: str = ""
    auth: bool = False
    auth_detail: str = ""
    sub: str = ""
    refs: int = 1
    error: str = ""


@dataclass
class Edge:
    """src -> dst. ``kind`` is pipeline | route | call | nested | body."""

    src: str
    dst: str
    kind: str = "call"


@dataclass
class Diagram:
    title: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def node(self, node_id: str) -> Node | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def routes(self) -> list[Node]:
        return [n for n in self.nodes if n.kind in ("route", "ws")]

    def deps(self) -> list[Node]:
        return [n for n in self.nodes if n.kind == "dep"]

    def schemas(self) -> list[Node]:
        return [n for n in self.nodes if n.kind == "schema"]

    def to_dict(self) -> dict:
        return asdict(self)


# ---- build ----


def build_diagram(app: Any, title: str | None = None, include_deps: bool = True) -> Diagram:
    """Snapshot an app's structure into a deterministic graph.

    Declared routes only: probe/docs routes are mounted lazily by the server,
    and ``_ikarem_internal`` routes (MCP transport) are implementation noise.
    Set ``include_deps=False`` for a route-only map.
    """
    from .compiled import get_plan

    graph = Diagram(title=title or "IKAREM app")
    router = getattr(app, "router", None)
    http_routes = [
        r for r in getattr(router, "routes", []) if not getattr(r.handler, "_ikarem_internal", False)
    ]
    ws_router = getattr(app, "ws_router", None)
    ws_routes = list(getattr(ws_router, "routes", []))

    segments: dict[str, int] = {}
    for r in [*http_routes, *ws_routes]:
        seg = _first_segment(r.path)
        if seg:
            segments[seg] = segments.get(seg, 0) + 1

    def group_of(route: Any) -> str:
        blueprint = getattr(route.handler, "_ikarem_blueprint", None)
        if isinstance(blueprint, str) and blueprint:
            return blueprint
        seg = _first_segment(route.path)
        if seg and segments.get(seg, 0) > 1:
            return seg
        return "app"

    route_ids: list[str] = []
    dep_ids: dict[int, str] = {}
    schema_ids: dict[int, str] = {}
    dep_refs: dict[str, int] = {}

    def add_dep(node: Any, parent: str, kind: str) -> None:
        if node is None or getattr(node, "is_primitive", False):
            return
        key = int(getattr(node, "key", 0) or id(node))
        nid = dep_ids.get(key)
        if nid is None:
            nid = f"d{len(dep_ids)}"
            dep_ids[key] = nid
            auth_detail = _dep_auth_detail(node)
            graph.nodes.append(
                Node(
                    id=nid,
                    label=_dep_label(node),
                    kind="dep",
                    auth=bool(getattr(node, "is_auth", False)),
                    auth_detail=auth_detail,
                    sub=str(getattr(node, "kind", "") or "sync"),
                    detail=_dep_tooltip(node),
                )
            )
        dep_refs[nid] = dep_refs.get(nid, 0) + 1
        graph.edges.append(Edge(parent, nid, kind))
        for sub in getattr(node, "sub_params", ()):
            if getattr(sub, "kind", "") == "nested" and getattr(sub, "nested", None) is not None:
                add_dep(sub.nested, nid, "nested")

    for route in http_routes:
        group = group_of(route)
        if group not in graph.groups:
            graph.groups.append(group)
        handler = route.handler
        try:
            plan = get_plan(handler)
        except Exception as exc:  # plans never raise by contract; stay safe anyway
            plan = None
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = plan.compile_error or ""
        view = getattr(handler, "__view_class__", None)
        handler_name = (
            getattr(view, "__name__", "") if view is not None else getattr(plan, "handler_name", "")
        ) or getattr(handler, "__name__", "handler")
        methods = _method_text(route.methods)
        node_id = f"r{len(route_ids)}"
        route_ids.append(node_id)
        auth = bool(getattr(plan, "is_auth", False))
        auth_detail = _plan_auth_detail(plan) if auth else ""
        body_cls = None
        if plan is not None and not plan.is_legacy_request_only:
            body_cls = next(
                (p.annotation for p in plan.params if p.kind == "schema"),
                None,
            )
        detail = _route_tooltip(route, plan, handler_name, body_cls, view)
        graph.nodes.append(
            Node(
                id=node_id,
                label=route.path,
                kind="route",
                group=group,
                method=methods,
                path=route.path,
                handler=handler_name,
                detail=detail,
                auth=auth,
                auth_detail=auth_detail,
                sub="MethodView" if view is not None else "",
                error=error,
            )
        )
        if include_deps and plan is not None and not plan.is_legacy_request_only:
            for hp in plan.params:
                if hp.kind == "depends" and hp.dep is not None:
                    add_dep(hp.dep, node_id, "call")
        if body_cls is not None:
            cls_key = id(body_cls)
            sid = schema_ids.get(cls_key)
            if sid is None:
                sid = f"s{len(schema_ids)}"
                schema_ids[cls_key] = sid
                graph.nodes.append(
                    Node(
                        id=sid,
                        label=getattr(body_cls, "__name__", "Schema"),
                        kind="schema",
                        sub=_schema_fields(body_cls),
                        detail=_schema_tooltip(body_cls),
                    )
                )
            graph.edges.append(Edge(node_id, sid, "body"))

    for route in ws_routes:
        group = group_of(route)
        if group not in graph.groups:
            graph.groups.append(group)
        node_id = f"w{len(route_ids)}"
        route_ids.append(node_id)
        handler_name = getattr(route.handler, "__name__", "socket")
        graph.nodes.append(
            Node(
                id=node_id,
                label=route.path,
                kind="ws",
                group=group,
                method="WS",
                path=route.path,
                handler=handler_name,
                detail=f"{handler_name}\nWebSocket route (bypasses HTTP middleware)",
            )
        )

    for nid, refs in dep_refs.items():
        node = graph.node(nid)
        if node is not None:
            node.refs = refs

    # HTTP pipeline: client -> middleware... -> router -> each route.
    stack = list(getattr(getattr(app, "middleware", None), "stack", []))
    if not http_routes:
        stack = []
    graph.nodes.insert(0, Node(id="client", label="client", kind="entry"))
    previous = "client"
    for i, mw in enumerate(stack):
        mid = f"m{i}"
        graph.nodes.append(
            Node(
                id=mid,
                label=_middleware_label(mw),
                kind="middleware",
                detail=f"{_middleware_label(mw)}\nMiddleware runs before routing; short-circuits stay inside the onion.",
            )
        )
        graph.edges.append(Edge(previous, mid, "pipeline"))
        previous = mid
    graph.nodes.append(
        Node(
            id="hub",
            label="router",
            kind="hub",
            sub=f"{len(http_routes)} route(s)",
            detail="HTTP router (first match wins)",
        )
    )
    graph.edges.append(Edge(previous, "hub", "pipeline"))
    for nid in route_ids[: len(http_routes)]:
        graph.edges.append(Edge("hub", nid, "route"))
    if ws_routes:
        graph.nodes.append(
            Node(
                id="wshub",
                label="ws router",
                kind="hub",
                sub=f"{len(ws_routes)} socket(s)",
                detail="WebSocket router: connects before the HTTP middleware onion by design.",
            )
        )
        graph.edges.append(Edge("client", "wshub", "pipeline"))
        for nid in route_ids[len(http_routes) :]:
            graph.edges.append(Edge("wshub", nid, "route"))

    graph.counts = {
        "routes": len(http_routes),
        "websockets": len(ws_routes),
        "groups": len(graph.groups),
        "dependencies": len(dep_ids) if include_deps else 0,
        "schemas": len(schema_ids),
        "middleware": len(stack),
        "auth_routes": sum(1 for n in graph.nodes if n.kind == "route" and n.auth),
        "shared_deps": sum(1 for n in graph.nodes if n.kind == "dep" and n.refs > 1),
        "edges": len(graph.edges),
    }
    return graph


def _first_segment(path: str) -> str:
    stripped = (path or "/").strip("/")
    return stripped.split("/", 1)[0] if stripped else ""


def _method_text(methods: Any) -> str:
    ordered = sorted(
        (m.upper() for m in methods),
        key=lambda m: (_METHOD_ORDER.index(m) if m in _METHOD_ORDER else 99, m),
    )
    return ",".join(ordered) or "HTTP"


def _plan_auth_detail(plan: Any) -> str:
    parts = [
        *(getattr(plan, "auth_roles", ()) or ()),
        *(getattr(plan, "auth_scopes", ()) or ()),
    ]
    if parts:
        return ",".join(str(p) for p in parts)
    return str(getattr(plan, "auth_scheme", "") or "required")


def _dep_auth_detail(node: Any) -> str:
    parts = [
        *(getattr(node, "auth_roles", ()) or ()),
        *(getattr(node, "auth_scopes", ()) or ()),
    ]
    if parts:
        return ",".join(str(p) for p in parts)
    header = str(getattr(node, "auth_header", "") or "")
    if header:
        return header
    return str(getattr(node, "auth_scheme", "") or "required")


def _dep_label(node: Any) -> str:
    if getattr(node, "is_auth", False):
        parts = [
            *(getattr(node, "auth_roles", ()) or ()),
            *(getattr(node, "auth_scopes", ()) or ()),
        ]
        scheme = str(getattr(node, "auth_scheme", "") or "")
        if parts:
            return f"auth:{'/'.join(str(p) for p in parts)}"
        if scheme:
            return f"auth:{scheme}"
        return "auth"
    display = str(getattr(node, "display", "") or "")
    fn = getattr(node, "fn", None)
    if not display or " at 0x" in display:
        display = type(fn).__name__ if fn is not None else "dep"
    return _clip(display, 34)


def _dep_tooltip(node: Any) -> str:
    lines = [f"dependency: {_dep_label(node)}"]
    kind = str(getattr(node, "kind", "") or "")
    if kind:
        lines.append(f"call style: {kind}")
    if getattr(node, "is_auth", False):
        detail = _dep_auth_detail(node)
        lines.append(f"auth boundary: {detail}")
    if getattr(node, "uses_config_secret", False):
        lines.append("reads auth_secret from app config")
    if getattr(node, "error", ""):
        lines.append(f"error: {node.error}")
    return "\n".join(lines)


def _schema_fields(cls: Any) -> str:
    try:
        from .validation import schema_json_schema

        props = list((schema_json_schema(cls).get("properties") or {}).keys())
    except Exception:
        props = [n for n in getattr(cls, "__annotations__", {}) if not n.startswith("_")]
    if not props:
        return "body"
    text = ", ".join(props[:5])
    return text + ("..." if len(props) > 5 else "")


def _schema_tooltip(cls: Any) -> str:
    name = getattr(cls, "__name__", "Schema")
    fields = _schema_fields(cls)
    return f"request body: {name}\nfields: {fields}"


def _route_tooltip(route: Any, plan: Any, handler_name: str, body_cls: Any, view: Any) -> str:
    lines = [f"{handler_name}  ({_method_text(route.methods)} {route.path})"]
    doc = inspect.getdoc(route.handler) or ""
    if doc:
        lines.append(doc.split("\n")[0][:160])
    if view is not None:
        lines.append(f"MethodView: {getattr(view, '__name__', 'view')}")
    if getattr(plan, "is_auth", False):
        lines.append(f"auth: {_plan_auth_detail(plan)}")
    if body_cls is not None:
        lines.append(f"body: {getattr(body_cls, '__name__', 'Schema')}")
    if getattr(plan, "has_background", False):
        lines.append("uses BackgroundTasks")
    if getattr(plan, "compile_error", ""):
        lines.append(f"compile error: {plan.compile_error}")
    for warning in (getattr(plan, "warnings", ()) or ())[:2]:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


def _middleware_label(mw: Any) -> str:
    name = getattr(mw, "__name__", "")
    return _clip(name or type(mw).__name__, 32)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# ---- renderers ----


def render_diagram(app: Any, fmt: str = "mermaid", **options: Any) -> str:
    """Build and render in one call. ``fmt`` is one of :data:`FORMATS`."""
    return render(build_diagram(app, **options), fmt)


def render(graph: Diagram, fmt: str = "mermaid") -> str:
    """Render an already-built graph. Unknown formats name the valid set."""
    key = (fmt or "mermaid").lower()
    renderers = {
        "mermaid": render_mermaid,
        "svg": render_svg,
        "html": render_html,
        "dot": render_dot,
        "ascii": render_ascii,
        "json": render_json,
    }
    if key not in renderers:
        raise ValueError(f"unknown diagram format {fmt!r}; choose one of: {', '.join(FORMATS)}")
    return renderers[key](graph)


def format_for_path(path: str | Path) -> str:
    """Pick a format from a filename extension (``.svg`` -> svg)."""
    ext = Path(path).suffix.lower()
    return _EXT_FORMATS.get(ext, "mermaid")


# ---- mermaid ----


def _mm(text: str) -> str:
    """Safe inside a double-quoted Mermaid label."""
    return " ".join(str(text).split()).replace('"', "'").replace("<", "&lt;").replace(">", "&gt;")


def render_mermaid(graph: Diagram) -> str:
    """GitHub-native flowchart. Subgraphs = route groups; dashed = nesting."""
    ids: dict[str, str] = {}
    for i, group in enumerate(graph.groups):
        ids[group] = f"g{i}"
    lines = [
        "%% Generated by `ikarem diagram` - edit code, not this file.",
        "flowchart LR",
        '  client(["client"])',
    ]
    previous = "client"
    for n in graph.nodes:
        if n.kind == "middleware":
            lines.append(f'  {n.id}["{_mm(n.label)}"]')
            lines.append(f"  {previous} --> {n.id}")
            previous = n.id
    lines.append('  hub{"router"}')
    lines.append(f"  {previous} --> hub")
    if any(n.kind == "ws" for n in graph.nodes):
        lines.append('  wshub{"ws router"}')
        lines.append("  client --> wshub")

    for group in graph.groups:
        lines.append(f'  subgraph {ids[group]}["{_mm(group)}"]')
        lines.append("    direction TB")
        for n in graph.nodes:
            if n.kind in ("route", "ws") and n.group == group:
                label = _mm(f"{n.method} {n.path}")
                second = _mm(n.handler)
                lines.append(f'    {n.id}["{label}<br/><small>{second}</small>"]')
        lines.append("  end")

    for n in graph.nodes:
        if n.kind == "dep":
            sub = f"used by {n.refs}" if n.refs > 1 else n.sub
            lines.append(f'  {n.id}(["{_mm(n.label)}<br/><small>{_mm(sub)}</small>"])')
        elif n.kind == "schema":
            lines.append(f'  {n.id}[/"{_mm(n.label)} body"/]')

    for e in graph.edges:
        if e.kind == "nested":
            lines.append(f"  {e.src} -.-> {e.dst}")
        elif e.kind == "body":
            lines.append(f"  {e.src} --> {e.dst}")
        elif e.kind in ("call", "route"):
            lines.append(f"  {e.src} --> {e.dst}")

    lines.extend(
        [
            "  classDef entry fill:#0f172a,stroke:#0f172a,color:#ffffff;",
            "  classDef hub fill:#1e293b,stroke:#1e293b,color:#ffffff;",
            "  classDef mw fill:#fef3c7,stroke:#b45309,color:#78350f;",
            "  classDef route fill:#ffffff,stroke:#475569,color:#0f172a;",
            "  classDef auth fill:#fff1f2,stroke:#be123c,color:#881337;",
            "  classDef ws fill:#f5f3ff,stroke:#6d28d9,color:#4c1d95;",
            "  classDef dep fill:#eef2ff,stroke:#4f46e5,color:#312e81;",
            "  classDef schema fill:#ecfdf5,stroke:#047857,color:#064e3b;",
            "  class client entry;",
            "  class hub hub;",
        ]
    )
    if any(n.kind == "ws" for n in graph.nodes):
        lines.append("  class wshub hub;")
    for n in graph.nodes:
        if n.kind == "middleware":
            lines.append(f"  class {n.id} mw;")
        elif n.kind == "route":
            lines.append(f"  class {n.id} {'auth' if n.auth else 'route'};")
        elif n.kind == "ws":
            lines.append(f"  class {n.id} ws;")
        elif n.kind == "dep":
            lines.append(f"  class {n.id} {'auth' if n.auth else 'dep'};")
        elif n.kind == "schema":
            lines.append(f"  class {n.id} schema;")
    if graph.counts:
        c = graph.counts
        lines.append(
            f"  %% {c['routes']} route(s), {c['dependencies']} dep(s), "
            f"{c['middleware']} middleware, {c['auth_routes']} auth route(s)"
        )
    return "\n".join(lines) + "\n"


# ---- dot ----


def _dot(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_dot(graph: Diagram) -> str:
    """Graphviz source. Clusters for groups; dotted nested-dep edges."""
    lines = [
        "// Generated by `ikarem diagram` - edit code, not this file.",
        "digraph ikarem {",
        '  graph [rankdir=LR, fontname="Helvetica", fontsize=11, pad=0.3, nodesep=0.25, ranksep=0.7];',
        '  node [fontname="monospace", fontsize=10, style="rounded,filled"];',
        '  edge [fontname="Helvetica", fontsize=9, color="#64748b", arrowsize=0.7];',
        f'  label="{_dot(graph.title)}";',
        '  labelloc="t";',
        '  client [label="client", shape=box, fillcolor="#0f172a", fontcolor="#ffffff", color="#0f172a"];',
    ]
    previous = "client"
    for n in graph.nodes:
        if n.kind == "middleware":
            lines.append(
                f'  {n.id} [label="{_dot(n.label)}", shape=box3d, fillcolor="#fef3c7", color="#b45309"];'
            )
            lines.append(f'  {previous} -> {n.id} [color="#f59e0b"];')
            previous = n.id
    for i, group in enumerate(graph.groups):
        if not any(n.kind in ("route", "ws") and n.group == group for n in graph.nodes):
            continue
        lines.append(f"  subgraph cluster_{i} {{")
        lines.append(f'    label="{_dot(group)}";')
        lines.append('    style="rounded,dashed"; color="#94a3b8"; fontname="Helvetica";')
        for n in graph.nodes:
            if n.kind in ("route", "ws") and n.group == group:
                fill = "#fff1f2" if n.auth else ("#f5f3ff" if n.kind == "ws" else "#ffffff")
                color = "#be123c" if n.auth else ("#6d28d9" if n.kind == "ws" else "#475569")
                label = f"{n.method} {n.path}\\n{n.handler}"
                if n.auth:
                    label += f"\\n[auth: {n.auth_detail}]"
                lines.append(
                    f'    {n.id} [label="{_dot(label)}", shape=box, fillcolor="{fill}", color="{color}"];'
                )
        lines.append("  }")
    lines.append(
        '  hub [label="router", shape=box, style="rounded,filled", fillcolor="#1e293b", fontcolor="#ffffff", color="#1e293b"];'
    )
    lines.append(f'  {previous} -> hub [color="#f59e0b"];')
    if any(n.kind == "ws" for n in graph.nodes):
        lines.append(
            '  wshub [label="ws router", shape=box, style="rounded,filled", fillcolor="#1e293b", fontcolor="#ffffff", color="#1e293b"];'
        )
        lines.append('  client -> wshub [color="#f59e0b"];')
    for n in graph.nodes:
        if n.kind == "dep":
            fill = "#fff1f2" if n.auth else "#eef2ff"
            color = "#be123c" if n.auth else "#4f46e5"
            sub = f"used by {n.refs}" if n.refs > 1 else n.sub
            lines.append(
                f'  {n.id} [label="{_dot(n.label)}\\n{_dot(sub)}", shape=ellipse, fillcolor="{fill}", color="{color}"];'
            )
        elif n.kind == "schema":
            lines.append(
                f'  {n.id} [label="{_dot(n.label)}\\nbody", shape=note, fillcolor="#ecfdf5", color="#047857"];'
            )
    for e in graph.edges:
        if e.src in ("client", "hub", "wshub") and e.kind in ("pipeline", "route"):
            style = ' [color="#f59e0b" style=dashed]' if e.kind == "pipeline" else ' [color="#94a3b8"]'
            lines.append(f"  {e.src} -> {e.dst}{style};")
        elif e.kind == "nested":
            lines.append(f'  {e.src} -> {e.dst} [style=dotted, color="#a5b4fc"];')
        elif e.kind == "body":
            lines.append(f'  {e.src} -> {e.dst} [color="#10b981"];')
        elif e.kind == "call":
            lines.append(f"  {e.src} -> {e.dst};")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ---- ascii ----


def render_ascii(graph: Diagram, width: int = 100) -> str:
    """Terminal map. Pure ASCII so cp1252 pipes never raise."""
    width = max(60, int(width))
    c = graph.counts
    lines = [
        graph.title,
        f"{c.get('routes', 0)} route(s) | {c.get('groups', 0)} group(s) | "
        f"{c.get('dependencies', 0)} dep(s) | {c.get('middleware', 0)} middleware | "
        f"{c.get('auth_routes', 0)} auth route(s)",
        "",
    ]
    mws = [n.label for n in graph.nodes if n.kind == "middleware"]
    lines.append("pipeline: client -> " + (" -> ".join(mws) + " -> " if mws else "") + "router")
    if c.get("websockets"):
        lines.append("ws:       client -> ws router (bypasses HTTP middleware)")
    lines.append("")
    for group in graph.groups:
        group_routes = [n for n in graph.nodes if n.kind in ("route", "ws") and n.group == group]
        if not group_routes:
            continue
        lines.append(f"{group} ({len(group_routes)})")
        for n in group_routes:
            head = f"  {n.method:<8} {n.path}"
            handler = n.handler
            auth = f" [auth:{n.auth_detail}]" if n.auth else ""
            error = " [ERROR]" if n.error else ""
            lines.append(_clip_width(f"{head:<48} -> {handler}{auth}{error}", width))
            route_deps = _route_deps(graph, n.id)
            if route_deps:
                lines.append(_clip_width("           deps: " + ", ".join(route_deps), width))
        lines.append("")
    shared = [n for n in graph.nodes if n.kind == "dep" and n.refs > 1]
    if shared:
        lines.append("shared dependencies")
        for n in shared:
            auth = f" [{n.auth_detail}]" if n.auth else ""
            lines.append(_clip_width(f"  {n.label:<32} used by {n.refs}{auth}", width))
        lines.append("")
    schemas = [n for n in graph.nodes if n.kind == "schema"]
    if schemas:
        lines.append("request bodies")
        for n in schemas:
            lines.append(_clip_width(f"  {n.label:<24} {n.sub}", width))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _route_deps(graph: Diagram, route_id: str) -> list[str]:
    out = []
    for e in graph.edges:
        if e.src == route_id and e.kind == "call":
            node = graph.node(e.dst)
            if node is not None:
                out.append(node.label + (f"[{node.auth_detail}]" if node.auth else ""))
    return out


def _clip_width(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


# ---- svg ----


_PAD = 30
_TITLE_H = 74
_LEGEND_H = 42
_ROUTE_H = 48
_ROUTE_GAP = 10
_GROUP_HEAD = 26
_GROUP_GAP = 20
_SMALL_H = 42


def _tw(text: str, size: float = 12) -> float:
    """Monospace width estimate (no font metrics without a dependency)."""
    return len(str(text)) * size * 0.62


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def render_svg(graph: Diagram) -> str:
    """Standalone SVG: layered layout, hand-routed cubic edges."""
    body, width, height = _svg_markup(graph)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(graph.title)}">\n'
        f"{body}</svg>\n"
    )


def _svg_markup(graph: Diagram) -> tuple[str, int, int]:
    groups: dict[str, list[Node]] = {g: [] for g in graph.groups}
    for n in graph.nodes:
        if n.kind in ("route", "ws") and n.group in groups:
            groups[n.group].append(n)

    path_text = {n.id: _clip(n.path, 44) for n in graph.nodes}
    handler_text = {n.id: _clip(n.handler, 34) for n in graph.nodes}

    def route_width(n: Node) -> float:
        method_w = _tw(n.method, 10.5) + 10
        base = 20 + method_w + _tw(path_text[n.id], 12) + 22
        base = max(base, 20 + _tw(handler_text[n.id], 11) + 22)
        if n.auth:
            base += _tw("auth:" + n.auth_detail, 10) + 18
        if n.error:
            base += 46
        return min(max(base, 200), 430)

    group_width: dict[str, float] = {}
    for group, nodes in groups.items():
        group_width[group] = max((route_width(n) for n in nodes), default=200) + 24

    route_w = max(group_width.values(), default=240)
    dep_list = [n for n in graph.nodes if n.kind == "dep"]
    schema_list = [n for n in graph.nodes if n.kind == "schema"]
    side_w = max(
        [_tw(_clip(n.label, 30), 12) + 32 for n in dep_list]
        + [_tw(_clip(n.label, 26), 12) + 32 for n in schema_list]
        + [_tw(_clip(n.sub, 30), 9.5) + 32 for n in (*dep_list, *schema_list)]
        + [170]
    )
    side_w = min(side_w, 300)

    mw_list = [n for n in graph.nodes if n.kind == "middleware"]
    pipeline_w = max([_tw(n.label, 11) + 36 for n in mw_list] + [_tw("client", 12) + 44, 150])

    x_client = _PAD
    x_routes = x_client + pipeline_w + 110
    x_side = x_routes + route_w + 120
    legend_w = 40 + 5 * 70 + 260
    width = int(max(820, x_side + side_w + _PAD, legend_w + _PAD))

    top = _TITLE_H
    y = top
    route_y: dict[str, float] = {}
    group_boxes: list[tuple[str, float, float, float]] = []
    for group in graph.groups:
        nodes = groups.get(group, [])
        if not nodes:
            continue
        box_top = y
        y += _GROUP_HEAD
        for n in nodes:
            route_y[n.id] = y
            y += _ROUTE_H + _ROUTE_GAP
        y -= _ROUTE_GAP
        group_boxes.append((group, box_top, group_width[group], y - box_top + 12))
        y += _GROUP_GAP
    routes_bottom = y
    content_bottom = max(routes_bottom, top + 190)

    # Side column: dependencies, then body schemas; anchor on first use.
    first_use: dict[str, float] = {}
    for e in graph.edges:
        if e.dst not in route_y:
            continue
        first_use[e.dst] = min(first_use.get(e.dst, 1e9), route_y[e.dst])
    side_pos: dict[str, float] = {}
    cursor = top
    for n in dep_list:
        anchor = first_use.get(n.id, cursor)
        node_y = max(anchor, cursor)
        side_pos[n.id] = node_y
        cursor = node_y + _SMALL_H + 12
    if dep_list and schema_list:
        cursor += 8
    for n in schema_list:
        anchor = first_use.get(n.id, cursor)
        node_y = max(anchor, cursor)
        side_pos[n.id] = node_y
        cursor = node_y + _SMALL_H + 12
    content_bottom = max(content_bottom, cursor)

    # Pipeline column: client -> middleware -> router -> (optional) ws router.
    pipe_pos: dict[str, float] = {"client": top}
    py = top + 58
    for n in mw_list:
        pipe_pos[n.id] = py
        py += 56
    pipe_pos["hub"] = py
    py += 58
    if any(n.kind == "ws" for n in graph.nodes):
        pipe_pos["wshub"] = py
        py += 58
    content_bottom = max(content_bottom, py)
    height = int(content_bottom + _LEGEND_H + _PAD)

    out: list[str] = [
        f"<title>{_esc(graph.title)}</title>",
        "<defs>",
    ]
    for kind, color in _EDGE_COLORS.items():
        out.append(
            f'<marker id="arrow-{kind}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/></marker>'
        )
    out.extend(
        [
            "</defs>",
            "<style>",
            "  text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
            "  .node .box{stroke-width:1.2}",
            "  .edge{fill:none;stroke-width:1.2;opacity:.55}",
            "  .edge-nested{stroke-dasharray:4 3}",
            "  .edge-body{opacity:.45}",
            "  .edge-pipeline{opacity:.8;stroke-width:1.6}",
            "  .group-box{fill:#94a3b8;fill-opacity:.05;stroke:#94a3b8;stroke-dasharray:5 4}",
            "  .group-label{fill:#64748b;font-size:10px;letter-spacing:1.4px;font-weight:700}",
            "  .method{font-size:10.5px;font-weight:700}",
            "  .path{fill:#0f172a;font-size:12px;font-weight:600}",
            "  .handler{fill:#64748b;font-size:11px}",
            "  .badge{font-size:9.5px;font-weight:700}",
            "  .sub{fill:#64748b;font-size:9.5px}",
            "  .dim{opacity:.12}",
            "</style>",
        ]
    )

    out.append('<g class="edges">')
    for e in graph.edges:
        src, dst = graph.node(e.src), graph.node(e.dst)
        if src is None or dst is None:
            continue
        if src.id in route_y:
            x1 = x_routes + route_w
            y1 = route_y[src.id] + _ROUTE_H / 2
        elif src.id in pipe_pos:
            x1 = x_client + pipeline_w
            y1 = pipe_pos[src.id] + 21
        else:
            continue
        if dst.id in route_y:
            x2 = x_routes
            y2 = route_y[dst.id] + _ROUTE_H / 2
        elif dst.id in side_pos:
            x2 = x_side
            y2 = side_pos[dst.id] + _SMALL_H / 2
        elif dst.id in pipe_pos:
            x2 = x_client
            y2 = pipe_pos[dst.id] + 21
        else:
            continue
        color = _EDGE_COLORS.get(e.kind, "#94a3b8")
        dash = ' stroke-dasharray="4 3"' if e.kind == "nested" else ""
        path = _curve(x1, y1, x2, y2)
        out.append(
            f'<path class="edge edge-{e.kind}" data-src="{src.id}" data-dst="{dst.id}" '
            f'd="{path}" stroke="{color}"{dash} marker-end="url(#arrow-{e.kind})"/>'
        )
    out.append("</g>")

    out.append('<g class="groups">')
    for group, gy, gw, gh in group_boxes:
        out.append(
            f'<rect class="group-box" x="{x_routes - 12}" y="{gy}" width="{gw:.0f}" '
            f'height="{gh:.0f}" rx="10"/>'
        )
        out.append(f'<text class="group-label" x="{x_routes}" y="{gy + 17}">{_esc(group)}</text>')
    out.append("</g>")

    out.append('<g class="nodes">')
    for n in graph.nodes:
        if n.kind in ("route", "ws"):
            out.append(_svg_route(n, x_routes, route_y[n.id], route_w, path_text, handler_text))
        elif n.kind == "dep":
            out.append(_svg_dep(n, x_side, side_pos[n.id], side_w))
        elif n.kind == "schema":
            out.append(_svg_schema(n, x_side, side_pos[n.id], side_w))
    for n in graph.nodes:
        if n.kind in ("entry", "middleware", "hub"):
            out.append(_svg_pipe(n, x_client, pipe_pos[n.id], pipeline_w))
    out.append("</g>")

    out.append(_svg_legend(graph, width, height))
    return "\n".join(out), width, height


def _curve(x1: float, y1: float, x2: float, y2: float) -> str:
    dx = max(30.0, abs(x2 - x1) * 0.45)
    return f"M {x1:.1f} {y1:.1f} C {x1 + dx:.1f} {y1:.1f}, {x2 - dx:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"


def _svg_route(
    n: Node,
    x: float,
    y: float,
    w: float,
    path_text: dict[str, str],
    handler_text: dict[str, str],
) -> str:
    method_color = _METHOD_COLORS.get(n.method.split(",")[0], "#475569")
    fill = "#fff1f2" if n.auth else ("#f5f3ff" if n.kind == "ws" else "#ffffff")
    stroke = "#be123c" if n.auth else ("#6d28d9" if n.kind == "ws" else ("#dc2626" if n.error else "#cbd5e1"))
    parts = [
        f'<g class="node route" id="{n.id}" data-search="{_esc((n.method + " " + n.path + " " + n.handler + " " + n.auth_detail).lower())}">',
        f"<title>{_esc(n.detail)}</title>",
        f'<rect class="box" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{_ROUTE_H}" rx="8" fill="{fill}" stroke="{stroke}"/>',
        f'<rect x="{x:.0f}" y="{y + 1:.0f}" width="4" height="{_ROUTE_H - 2}" rx="2" fill="{method_color}"/>',
        f'<text class="method" x="{x + 18:.0f}" y="{y + 20:.0f}" fill="{method_color}">{_esc(n.method)}</text>',
        f'<text class="path" x="{x + 20 + _tw(n.method, 10.5):.0f}" y="{y + 20:.0f}">{_esc(path_text[n.id])}</text>',
        f'<text class="handler" x="{x + 18:.0f}" y="{y + 37:.0f}">{_esc(handler_text[n.id])}</text>',
    ]
    if n.auth:
        parts.append(
            f'<text class="badge" x="{x + w - 10:.0f}" y="{y + 20:.0f}" fill="#be123c" '
            f'text-anchor="end">auth:{_esc(_clip(n.auth_detail, 20))}</text>'
        )
    if n.error:
        parts.append(
            f'<text class="badge" x="{x + w - 10:.0f}" y="{y + 37:.0f}" fill="#dc2626" '
            f'text-anchor="end">ERROR</text>'
        )
    parts.append("</g>")
    return "\n".join(parts)


def _svg_dep(n: Node, x: float, y: float, w: float) -> str:
    fill = "#fff1f2" if n.auth else "#eef2ff"
    stroke = "#be123c" if n.auth else "#818cf8"
    sub = f"used by {n.refs}" if n.refs > 1 else n.sub
    parts = [
        f'<g class="node dep" id="{n.id}" data-search="{_esc((n.label + " " + n.sub).lower())}">',
        f"<title>{_esc(n.detail)}</title>",
        f'<rect class="box" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{_SMALL_H}" rx="12" fill="{fill}" stroke="{stroke}"/>',
        f'<text class="path" x="{x + 14:.0f}" y="{y + 18:.0f}" fill="#312e81">{_esc(_clip(n.label, 30))}</text>',
        f'<text class="sub" x="{x + 14:.0f}" y="{y + 32:.0f}">{_esc(sub)}</text>',
    ]
    if n.auth:
        parts.append(
            f'<text class="badge" x="{x + w - 12:.0f}" y="{y + 18:.0f}" fill="#be123c" '
            f'text-anchor="end">AUTH</text>'
        )
    parts.append("</g>")
    return "\n".join(parts)


def _svg_schema(n: Node, x: float, y: float, w: float) -> str:
    return "\n".join(
        [
            f'<g class="node schema" id="{n.id}" data-search="{_esc((n.label + " " + n.sub).lower())}">',
            f"<title>{_esc(n.detail)}</title>",
            f'<rect class="box" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{_SMALL_H}" rx="8" fill="#ecfdf5" stroke="#34d399"/>',
            f'<text class="path" x="{x + 14:.0f}" y="{y + 18:.0f}" fill="#065f46">{_esc(_clip(n.label, 26))}</text>',
            f'<text class="sub" x="{x + 14:.0f}" y="{y + 32:.0f}">{_esc(_clip(n.sub, 30))}</text>',
            "</g>",
        ]
    )


def _svg_pipe(n: Node, x: float, y: float, w: float) -> str:
    if n.kind == "entry":
        fill, stroke, color = "#0f172a", "#0f172a", "#ffffff"
    elif n.kind == "hub":
        fill, stroke, color = "#1e293b", "#1e293b", "#ffffff"
    else:
        fill, stroke, color = "#fef3c7", "#f59e0b", "#78350f"
    h = 42
    return "\n".join(
        [
            f'<g class="node {n.kind}" id="{n.id}" data-search="{_esc(n.label.lower())}">',
            f"<title>{_esc(n.detail)}</title>",
            f'<rect class="box" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}"/>',
            f'<text class="path" x="{x + 14:.0f}" y="{y + 18:.0f}" fill="{color}">{_esc(_clip(n.label, 28))}</text>',
            (
                f'<text class="sub" x="{x + 14:.0f}" y="{y + 32:.0f}" fill="#cbd5e1">{_esc(n.sub)}</text>'
                if n.sub
                else ""
            ),
            "</g>",
        ]
    )


def _svg_legend(graph: Diagram, width: int, height: int) -> str:
    y = height - _LEGEND_H + 12
    x = _PAD
    parts = [f'<g class="legend" transform="translate(0,{y})">']
    parts.append(
        f'<text x="{x}" y="12" class="sub" fill="#475569">'
        f"{_esc(graph.counts.get('routes', 0))} routes | "
        f"{_esc(graph.counts.get('dependencies', 0))} deps | "
        f"{_esc(graph.counts.get('middleware', 0))} middleware | "
        f"{_esc(graph.counts.get('auth_routes', 0))} auth</text>"
    )
    x += 300
    for method, color in _METHOD_COLORS.items():
        if method in ("HEAD", "OPTIONS"):
            continue
        parts.append(f'<rect x="{x}" y="3" width="8" height="8" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{x + 12}" y="12" class="sub">{method}</text>')
        x += 70
    parts.append(
        f'<path d="M {x} 8 L {x + 22} 8" stroke="#64748b" stroke-width="1.4" stroke-dasharray="4 3" marker-end="url(#arrow-nested)"/>'
    )
    parts.append(f'<text x="{x + 28}" y="12" class="sub">nested dep</text>')
    parts.append("</g>")
    return "\n".join(parts)


# ---- html ----


def render_html(graph: Diagram) -> str:
    """Self-contained page: inline SVG, instant filter, zoom. No network."""
    body, width, height = _svg_markup(graph)
    title = _esc(graph.title)
    c = graph.counts
    meta = (
        f"{c.get('routes', 0)} routes &middot; {c.get('dependencies', 0)} dependencies &middot; "
        f"{c.get('middleware', 0)} middleware &middot; {c.get('auth_routes', 0)} auth"
    )
    svg = (
        f'<svg id="diagram" xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{title}">\n{body}</svg>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #f8fafc; color: #0f172a;
         font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; }}
  header {{ padding: 22px 30px 14px; border-bottom: 1px solid #e2e8f0; background: #fff; }}
  h1 {{ margin: 0; font-size: 20px; letter-spacing: -0.01em; }}
  .meta {{ margin: 4px 0 0; color: #64748b; font-size: 12px; }}
  .bar {{ position: sticky; top: 0; z-index: 2; display: flex; gap: 10px; align-items: center;
          padding: 10px 30px; background: rgba(255,255,255,.92); backdrop-filter: blur(6px);
          border-bottom: 1px solid #e2e8f0; }}
  input {{ flex: 1; max-width: 420px; padding: 8px 12px; border: 1px solid #cbd5e1;
           border-radius: 8px; font: inherit; font-size: 13px; }}
  input:focus {{ outline: 2px solid #93c5fd; border-color: #60a5fa; }}
  button {{ padding: 7px 11px; border: 1px solid #cbd5e1; border-radius: 8px; background: #fff;
            font: inherit; font-size: 12px; cursor: pointer; }}
  button:hover {{ background: #f1f5f9; }}
  .hint {{ color: #94a3b8; font-size: 12px; }}
  #wrap {{ overflow: auto; padding: 10px 20px 30px; }}
  #diagram {{ display: block; transform-origin: 0 0; }}
  footer {{ padding: 0 30px 24px; color: #94a3b8; font-size: 12px; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <p class="meta">{meta} &middot; hover any box for its contract</p>
</header>
<div class="bar">
  <input id="q" type="search" placeholder="Filter routes, handlers, dependencies..." autocomplete="off">
  <button type="button" data-zoom="0.15">+</button>
  <button type="button" data-zoom="-0.15">&minus;</button>
  <button type="button" data-zoom="0">reset</button>
  <span class="hint">dashed edge = nested dependency</span>
</div>
<div id="wrap">
{svg}
</div>
<footer>Generated by <code>ikarem diagram</code> from the app's compiled plans. Rerun after route changes; output is deterministic.</footer>
<script>
(function () {{
  var q = document.getElementById('q');
  var svg = document.getElementById('diagram');
  var nodes = Array.prototype.slice.call(svg.querySelectorAll('.node'));
  var edges = Array.prototype.slice.call(svg.querySelectorAll('.edge'));
  function apply() {{
    var needle = q.value.trim().toLowerCase();
    var hits = {{}};
    nodes.forEach(function (n) {{
      var hit = !needle || (n.getAttribute('data-search') || '').indexOf(needle) !== -1;
      if (hit) hits[n.id] = 1;
      n.classList.toggle('dim', !hit);
    }});
    edges.forEach(function (e) {{
      var match = hits[e.getAttribute('data-src')] && hits[e.getAttribute('data-dst')];
      e.classList.toggle('dim', !!needle && !match);
    }});
  }}
  q.addEventListener('input', apply);
  var baseW = {width}, baseH = {height}, scale = 1;
  document.querySelectorAll('[data-zoom]').forEach(function (b) {{
    b.addEventListener('click', function () {{
      var d = parseFloat(b.getAttribute('data-zoom'));
      scale = d === 0 ? 1 : Math.min(3, Math.max(0.4, scale + d));
      svg.setAttribute('width', String(Math.round(baseW * scale)));
      svg.setAttribute('height', String(Math.round(baseH * scale)));
    }});
  }});
}})();
</script>
</body>
</html>
"""


# ---- json ----


def render_json(graph: Diagram) -> str:
    """Stable machine-readable graph (snapshot tests, editors, agents)."""
    return json.dumps(graph.to_dict(), indent=1, sort_keys=False) + "\n"


__all__ = [
    "FORMATS",
    "Diagram",
    "Edge",
    "Node",
    "build_diagram",
    "format_for_path",
    "render",
    "render_ascii",
    "render_diagram",
    "render_dot",
    "render_html",
    "render_json",
    "render_mermaid",
    "render_svg",
]
