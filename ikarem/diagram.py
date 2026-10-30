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
    name = getattr(mw, "__name__", "") or type(mw).__name__
    for suffix in ("Middleware", "Manager"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return _clip(name, 32)


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


_PAD = 40
_LEGEND_H = 78
_CANVAS_W = 1320
_COL_GAP = 24
_CARD_GAP = 20
_ROW_H = 60
_ROW_GAP = 8
_CHIP_H = 32
_CHIP_GAP = 26


def _tw(text: str, size: float = 12) -> float:
    """Monospace width estimate (no font metrics without a dependency)."""
    return len(str(text)) * size * 0.62


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def render_svg(graph: Diagram) -> str:
    """Standalone SVG: two-column card map, pipeline band, dependency cards."""
    body, width, height = _svg_markup(graph)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(graph.title)}">\n'
        f"{body}</svg>\n"
    )


def _svg_markup(graph: Diagram, chrome: bool = True) -> tuple[str, int, int]:
    """Two-column card map: pipeline band, grouped route cards, contract cards.

    No long edges: each route row carries pills for the dependencies and body
    it uses, while dependency/body cards keep the sharing counts and auth
    boundaries. Cards flow into the shorter column (deterministic tie -> left),
    so the page uses both axes instead of one dense 2,500px column.
    """
    mw = [n for n in graph.nodes if n.kind == "middleware"]
    deps = graph.deps()
    schemas = graph.schemas()
    groups = {g: [] for g in graph.groups}
    for n in graph.nodes:
        if n.kind in ("route", "ws") and n.group in groups:
            groups[n.group].append(n)

    direct: dict[str, list[Node]] = {}
    body_of: dict[str, Node] = {}
    children: dict[str, list[str]] = {}
    for e in graph.edges:
        if e.kind == "call":
            node = graph.node(e.dst)
            if node is not None:
                direct.setdefault(e.src, []).append(node)
        elif e.kind == "body":
            node = graph.node(e.dst)
            if node is not None:
                body_of[e.src] = node
        elif e.kind == "nested":
            child = graph.node(e.dst)
            if child is not None:
                children.setdefault(e.src, []).append(child.label)

    left = _PAD
    width = _CANVAS_W
    col_w = (width - 2 * _PAD - _COL_GAP) // 2

    out: list[str] = [
        f"<title>{_esc(graph.title)}</title>",
        "<defs>",
        '<marker id="arrow-flow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        'markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" '
        'fill="#94a3b8"/></marker>',
        "</defs>",
        "<style>",
        "  text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
        "  .title{font-size:23px;font-weight:700;fill:#0f172a;letter-spacing:-.02em}",
        "  .meta{font-size:11.5px;fill:#64748b;font-family:ui-sans-serif,system-ui,sans-serif}",
        "  .lane{font-size:10.5px;font-weight:700;letter-spacing:1.7px;fill:#94a3b8}",
        "  .card{fill:#ffffff;stroke:#e2e8f0;stroke-width:1}",
        "  .group{font-size:10.5px;font-weight:700;letter-spacing:1.4px;fill:#475569}",
        "  .count{font-size:10px;fill:#94a3b8}",
        "  .row{fill:#ffffff;stroke:#e8edf4;stroke-width:1}",
        "  .path{font-size:12.5px;font-weight:600;fill:#0f172a}",
        "  .handler{font-size:10px;fill:#94a3b8}",
        "  .sub{font-size:10px;fill:#64748b}",
        "  .chip-text{font-size:10.5px;font-weight:700}",
        "  .pill-text{font-size:10px;font-weight:600}",
        "  .auth-text{font-size:9px;font-weight:700}",
        "  .dep-name{font-size:11.5px;font-weight:600;fill:#312e81}",
        "  .schema-name{font-size:11.5px;font-weight:600;fill:#065f46}",
        "  .rail-row{fill:#eef2ff;stroke:#e0e7ff}",
        "  .rail-row.auth{fill:#fff1f2;stroke:#fecdd3}",
        "  .rail-row.schema{fill:#ecfdf5;stroke:#d1fae5}",
        "  .edge{stroke:#94a3b8;stroke-width:1.2;fill:none}",
        "  .dim{opacity:.12}",
        "</style>",
    ]

    if chrome:
        out.append(f'<text class="title" x="{left}" y="38">{_esc(graph.title)}</text>')
        c = graph.counts
        meta = (
            f"{c.get('routes', 0)} routes  |  {c.get('groups', 0)} groups  |  "
            f"{c.get('dependencies', 0)} dependencies  |  {c.get('middleware', 0)} middleware  |  "
            f"{c.get('auth_routes', 0)} auth"
        )
        out.append(f'<text class="meta" x="{left}" y="60">{_esc(meta)}</text>')
        lane_y = 92
        pipe_y = 106
    else:
        lane_y = 22
        pipe_y = 36

    out.append(f'<text class="lane" x="{left}" y="{lane_y}">PIPELINE</text>')

    chips: list[tuple[str, str, str, str]] = [("client", "entry", "client", "HTTP client")]
    for n in mw:
        chips.append((n.label, "mw", n.id, n.label))
    chips.append(("router", "hub", "hub", "HTTP router"))
    available = width - 2 * _PAD
    rows: list[list[tuple[str, str, str, str]]] = []
    row: list[tuple[str, str, str, str]] = []
    used = 0.0
    for chip in chips:
        cw = _chip_width(chip[0])
        need = cw + (_CHIP_GAP if row else 0)
        if row and used + need > available:
            rows.append(row)
            row = []
            used = 0
            need = cw
        row.append(chip)
        used += need
    if row:
        rows.append(row)

    for ridx, prow in enumerate(rows):
        x = left
        prev_end: float | None = None
        prev_id: str | None = None
        for label, kind, node_id, title in prow:
            cw = _chip_width(label)
            if prev_end is not None:
                out.append(
                    f'<path class="edge" data-src="{prev_id}" data-dst="{node_id}" '
                    f'd="M {prev_end + 4:.0f} {pipe_y + _CHIP_H / 2:.0f} '
                    f'L {x - 6:.0f} {pipe_y + _CHIP_H / 2:.0f}" marker-end="url(#arrow-flow)"/>'
                )
            fill, stroke, color = _chip_style(kind)
            out.append(
                f'<g class="node {kind}" id="{node_id}" data-search="{_esc(label.lower())}">'
                f"<title>{_esc(title)}</title>"
                f'<rect x="{x:.0f}" y="{pipe_y}" width="{cw:.0f}" height="{_CHIP_H}" rx="7" '
                f'fill="{fill}" stroke="{stroke}"/>'
                f'<text class="chip-text" x="{x + cw / 2:.0f}" y="{pipe_y + 19}" '
                f'fill="{color}" text-anchor="middle">{_esc(_clip(label, 26))}</text></g>'
            )
            prev_end = x + cw
            prev_id = node_id
            x += cw + _CHIP_GAP
        if ridx + 1 < len(rows) and prev_end is not None:
            # Wrapped pipeline: elbow from the end of this row to the next row's
            # first chip, so the onion still reads as one continuous flow.
            mid = pipe_y + _CHIP_H / 2
            gap_y = pipe_y + _CHIP_H + 10
            next_mid = pipe_y + _CHIP_H + 20 + _CHIP_H / 2
            out.append(
                f'<path class="edge" data-src="{prev_id}" data-dst="{rows[ridx + 1][0][2]}" '
                f'd="M {prev_end + 4:.0f} {mid:.0f} H {prev_end + 16:.0f} V {gap_y:.0f} '
                f'H {left - 10:.0f} V {next_mid:.0f} H {left - 2:.0f}" '
                'marker-end="url(#arrow-flow)"/>'
            )
        pipe_y += _CHIP_H + 20

    if any(n.kind == "ws" for n in graph.nodes):
        out.append(
            f'<text class="meta" x="{left}" y="{pipe_y + 2}">'
            "WS   client &#8658; ws router &#8212; WebSocket routes bypass the HTTP middleware onion"
            "</text>"
        )
        pipe_y += 26

    content_top = pipe_y + 24
    c = graph.counts
    out.append(
        f'<text class="lane" x="{left}" y="{content_top}">'
        f"ROUTES &#183; {c.get('routes', 0)} HTTP + {c.get('websockets', 0)} WS</text>"
    )
    grid_top = content_top + 16

    specs: list[tuple[str, str, Any, int]] = []
    for group in graph.groups:
        nodes = groups.get(group, [])
        if nodes:
            h = 54 + len(nodes) * (_ROW_H + _ROW_GAP)
            specs.append(("group", group, nodes, h))
    if deps:
        specs.append(("deps", "", deps, 48 + len({n.label for n in deps}) * 50))
    if schemas:
        specs.append(("schemas", "", schemas, 48 + len(schemas) * 50))

    col_y = [grid_top, grid_top]
    placed: list[tuple[int, int, str, str, Any, int]] = []
    for kind, name, payload, h in specs:
        idx = 0 if col_y[0] <= col_y[1] else 1
        placed.append((idx, col_y[idx], kind, name, payload, h))
        col_y[idx] += h + _CARD_GAP

    for idx, y, kind, name, payload, h in placed:
        x = left + idx * (col_w + _COL_GAP)
        if kind == "group":
            _svg_group_card(out, name, payload, x, y, col_w, direct, body_of)
        elif kind == "deps":
            _svg_dep_card(out, payload, x, y, col_w, children)
        else:
            _svg_schema_card(out, payload, x, y, col_w)

    height = int(max(col_y) + _LEGEND_H + _PAD)
    out.append(_svg_legend(graph, left, height - _LEGEND_H + 10, width - 2 * _PAD))
    return "\n".join(out), width, height


def _chip_width(label: str) -> float:
    return min(220.0, max(64.0, _tw(label, 10.5) + 26))


def _chip_style(kind: str) -> tuple[str, str, str]:
    if kind == "entry":
        return "#0f172a", "#0f172a", "#ffffff"
    if kind == "hub":
        return "#1e293b", "#1e293b", "#ffffff"
    return "#fef3c7", "#f59e0b", "#78350f"


def _svg_group_card(
    out: list[str],
    group: str,
    nodes: list[Node],
    x: float,
    y: float,
    w: float,
    direct: dict[str, list[Node]],
    body_of: dict[str, Node],
) -> None:
    h = 54 + len(nodes) * (_ROW_H + _ROW_GAP)
    out.append(f'<rect class="card" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>')
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="44" rx="12" fill="#f8fafc"/>')
    out.append(f'<rect x="{x}" y="{y + 22}" width="{w}" height="22" fill="#f8fafc"/>')
    out.append(f'<line x1="{x}" y1="{y + 44}" x2="{x + w}" y2="{y + 44}" stroke="#eef2f6"/>')
    out.append(f'<text class="group" x="{x + 18}" y="{y + 27}">{_esc(group.upper())}</text>')
    out.append(f'<text class="count" x="{x + w - 18}" y="{y + 27}" text-anchor="end">{len(nodes)}</text>')
    ry = y + 50
    for n in nodes:
        out.append(_svg_row(n, x + 10, ry, w - 20, direct.get(n.id, []), body_of.get(n.id)))
        ry += _ROW_H + _ROW_GAP


def _svg_row(n: Node, x: float, y: float, w: float, deps: list[Node], body: Node | None) -> str:
    """Two clean lines per route: identity/auth on top, handler + pills below.

    Pills are right-aligned as one block so they never crowd the handler name,
    and repeated labels (the same dependency injected twice) collapse.
    """
    right = x + w
    method = n.method or "HTTP"
    method_w = min(96.0, max(42.0, _tw(method, 10) + 16))
    method_color = _METHOD_COLORS.get(method.split(",")[0], "#475569")
    stroke = "#fecdd3" if n.auth else ("#ddd6fe" if n.kind == "ws" else "#e8edf4")

    auth_text = ""
    auth_w = 0.0
    if n.auth:
        auth_text = f"auth:{_clip(n.auth_detail, 20)}"
        auth_w = _tw(auth_text, 9) + 18
    path_x = x + 11 + method_w + 10
    path_room = right - 11 - auth_w - path_x - 8
    path_text = _clip(n.path, max(8, int(path_room / 7.6)))

    handler = _clip(n.handler, 26)
    handler_w = _tw(handler, 10)

    pills: list[tuple[str, str]] = []
    seen: set[str] = set()
    for d in deps:
        if d.auth or d.label in seen:
            continue
        seen.add(d.label)
        pills.append((d.label, "dep"))
    if body is not None:
        pills.append((f"body {body.label}", "body"))

    budget = (right - 11 - handler_w - 18) - (x + 12)
    sized: list[tuple[str, str, float]] = []
    used = 0.0
    for label, kind in pills:
        label = _clip(label, 18)
        pw = _tw(label, 10) + 16
        need = pw + (6 if sized else 0)
        if used + need > budget:
            break
        sized.append((label, kind, pw))
        used += need
    hidden = len(pills) - len(sized)
    if hidden:
        more = f"+{hidden}"
        sized.append((more, "more", _tw(more, 10) + 16))
        used += 6 + _tw(more, 10) + 16

    pill_svg: list[str] = []
    px = right - 12 - used
    for label, kind, pw in sized:
        pill_svg.append(_pill(px, y + 33, label, kind))
        px += pw + 6

    search = " ".join(
        [
            method.lower(),
            n.path.lower(),
            n.handler.lower(),
            n.auth_detail.lower(),
            *(d.label.lower() for d in deps),
            *([body.label.lower()] if body is not None else []),
        ]
    )
    parts = [
        f'<g class="node route" id="{n.id}" data-search="{_esc(search)}">',
        f"<title>{_esc(n.detail)}</title>",
        f'<rect class="row" x="{x}" y="{y}" width="{w}" height="{_ROW_H}" rx="8" stroke="{stroke}"/>',
        f'<rect x="{x + 11}" y="{y + 9}" width="{method_w:.0f}" height="19" rx="4" fill="{method_color}"/>',
        f'<text class="pill-text" x="{x + 11 + method_w / 2:.0f}" y="{y + 22.5}" fill="#ffffff" '
        f'text-anchor="middle">{_esc(_clip(method, 12))}</text>',
        f'<text class="path" x="{path_x:.0f}" y="{y + 23}">{_esc(path_text)}</text>',
    ]
    if n.auth:
        ax = right - 11 - auth_w
        parts.append(
            f'<rect x="{ax:.0f}" y="{y + 8}" width="{auth_w:.0f}" height="19" rx="9.5" '
            'fill="#fff1f2" stroke="#fecdd3"/>'
        )
        parts.append(
            f'<text class="auth-text" x="{ax + auth_w / 2:.0f}" y="{y + 21}" fill="#be123c" '
            f'text-anchor="middle">{_esc(auth_text)}</text>'
        )
    parts.append(f'<text class="handler" x="{x + 12}" y="{y + 47}">{_esc(handler)}</text>')
    parts.extend(pill_svg)
    if n.error:
        parts.append(
            f'<text class="auth-text" x="{right - 11:.0f}" y="{y + 21}" fill="#dc2626" '
            f'text-anchor="end">ERROR</text>'
        )
    parts.append("</g>")
    return "\n".join(parts)


def _pill(x: float, y: float, label: str, kind: str) -> str:
    styles = {
        "dep": ("#eef2ff", "#c7d2fe", "#3730a3"),
        "body": ("#ecfdf5", "#a7f3d0", "#047857"),
        "more": ("#f1f5f9", "#e2e8f0", "#64748b"),
    }
    fill, stroke, color = styles.get(kind, styles["more"])
    w = _tw(label, 10) + 16
    return (
        f'<g class="pill"><rect x="{x:.0f}" y="{y}" width="{w:.0f}" height="18" rx="9" '
        f'fill="{fill}" stroke="{stroke}"/>'
        f'<text class="pill-text" x="{x + w / 2:.0f}" y="{y + 12.5}" fill="{color}" '
        f'text-anchor="middle">{_esc(label)}</text></g>'
    )


def _svg_dep_card(
    out: list[str],
    deps: list[Node],
    x: float,
    y: float,
    w: float,
    children: dict[str, list[str]],
) -> None:
    ordered = sorted(deps, key=lambda n: (-n.refs, n.id))
    merged: dict[str, list[Node]] = {}
    for n in ordered:
        merged.setdefault(n.label, []).append(n)
    h = 48 + len(merged) * 50
    out.append(f'<rect class="card" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>')
    out.append(f'<text class="group" x="{x + 18}" y="{y + 25}">DEPENDENCIES &#183; {len(ordered)}</text>')
    ry = y + 44
    for label, nodes in merged.items():
        kids: list[str] = []
        for n in nodes:
            for kid in children.get(n.id, []):
                if kid not in kids:
                    kids.append(kid)
        out.append(_svg_dep_row(label, nodes, x + 8, ry, w - 16, kids))
        ry += 50


def _svg_dep_row(label: str, nodes: list[Node], x: float, y: float, w: float, kids: list[str]) -> str:
    """One row per distinct dependency label (the same guard can compile twice)."""
    auth = any(n.auth for n in nodes)
    cls = "rail-row auth" if auth else "rail-row"
    refs = sum(n.refs for n in nodes)
    bits = [nodes[0].auth_detail] if auth and nodes[0].auth_detail else []
    bits.append(f"used by {refs}")
    sub = " &#183; ".join(bits)
    if kids:
        sub += " &#8594; " + ", ".join(kids[:3])
    dot = "#e11d48" if auth else "#6366f1"
    search = " ".join([label, *(n.sub for n in nodes)]).lower()
    return "\n".join(
        [
            f'<g class="node dep" id="{nodes[0].id}" data-search="{_esc(search)}">',
            f"<title>{_esc(nodes[0].detail)}</title>",
            f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="44" rx="8"/>',
            f'<circle cx="{x + 15}" cy="{y + 15}" r="3" fill="{dot}"/>',
            f'<text class="dep-name" x="{x + 26}" y="{y + 18}">{_esc(_clip(label, 30))}'
            f"{f' &#215;{len(nodes)}' if len(nodes) > 1 else ''}</text>",
            f'<text class="sub" x="{x + 26}" y="{y + 35}">{_esc(_clip(sub, 54))}</text>',
            "</g>",
        ]
    )


def _svg_schema_card(out: list[str], schemas: list[Node], x: float, y: float, w: float) -> None:
    h = 48 + len(schemas) * 50
    out.append(f'<rect class="card" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>')
    out.append(f'<text class="group" x="{x + 18}" y="{y + 25}">REQUEST BODIES &#183; {len(schemas)}</text>')
    ry = y + 44
    for s in schemas:
        out.append(_svg_schema_row(s, x + 8, ry, w - 16))
        ry += 50


def _svg_schema_row(n: Node, x: float, y: float, w: float) -> str:
    return "\n".join(
        [
            f'<g class="node schema" id="{n.id}" data-search="{_esc((n.label + " " + n.sub).lower())}">',
            f"<title>{_esc(n.detail)}</title>",
            f'<rect class="rail-row schema" x="{x}" y="{y}" width="{w}" height="44" rx="8"/>',
            f'<circle cx="{x + 15}" cy="{y + 15}" r="3" fill="#059669"/>',
            f'<text class="schema-name" x="{x + 26}" y="{y + 18}">{_esc(_clip(n.label, 32))}</text>',
            f'<text class="sub" x="{x + 26}" y="{y + 35}">{_esc(_clip(n.sub, 54))}</text>',
            "</g>",
        ]
    )


def _svg_legend(graph: Diagram, x: float, y: float, width: float) -> str:
    parts = ['<g class="legend">']
    parts.append(f'<line x1="{x}" y1="{y - 14}" x2="{x + width}" y2="{y - 14}" stroke="#e2e8f0"/>')
    cx = x
    for method, color in _METHOD_COLORS.items():
        if method in ("HEAD", "OPTIONS"):
            continue
        parts.append(f'<rect x="{cx}" y="{y}" width="8" height="8" rx="2" fill="{color}"/>')
        parts.append(f'<text class="sub" x="{cx + 12}" y="{y + 8}">{method}</text>')
        cx += 64
    cx += 10
    parts.append(_pill(cx, y - 6, "dependency", "dep"))
    cx += _tw("dependency", 10) + 16 + 10
    parts.append(_pill(cx, y - 6, "body", "body"))
    cx += _tw("body", 10) + 16 + 12
    parts.append(
        f'<text class="sub" x="{cx}" y="{y + 8}">'
        "pills = what each route uses &#183; full graph: ikarem diagram -f mermaid</text>"
    )
    parts.append("</g>")
    return "\n".join(parts)


# ---- html ----


def render_html(graph: Diagram) -> str:
    """Self-contained page: inline SVG, instant filter, zoom. No network."""
    body, width, height = _svg_markup(graph, chrome=False)
    title = _esc(graph.title)
    c = graph.counts
    meta = (
        f"{c.get('routes', 0)} routes &middot; {c.get('dependencies', 0)} dependencies &middot; "
        f"{c.get('middleware', 0)} middleware &middot; {c.get('auth_routes', 0)} auth"
    )
    if c.get("websockets"):
        meta += f" &middot; {c['websockets']} WebSocket"
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
  #wrap {{ overflow: auto; padding: 18px 20px 30px; }}
  #diagram {{ display: block; margin: 0 auto; transform-origin: 0 0; }}
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
  <span class="hint">pills show what each route uses &middot; full graph: <code>ikarem diagram -f mermaid</code></span>
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
