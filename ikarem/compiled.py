"""Compiled handler plans: parse each handler's DI graph ONCE, resolve with zero reflection.

Before: di.resolve_handler() called inspect.signature() on the handler AND every
nested dependency on EVERY request. After: compile_handler() runs inspect ONCE
(at route registration / startup); per-request resolution is dict lookups + calls.

Same file powers `ikarem check`: the plan exposes exactly what would fail at
runtime (circular deps, bare Depends(), untyped fallbacks) before serving traffic.
"""

from __future__ import annotations

import inspect
import re as _re
from dataclasses import dataclass, field
from typing import Any

from .validation import is_schema_like, schema_json_schema, validate_schema

_MISSING = object()


def _coerce_query(value: str, ann: Any) -> Any:
    if ann is int:
        return int(value)
    if ann is float:
        return float(value)
    if ann is bool:
        return value.lower() in ("1", "true", "yes", "on")
    return value


def _is_background_ann(ann: Any) -> bool:
    return inspect.isclass(ann) and getattr(ann, "__name__", "") == "BackgroundTasks"


def _is_schema_ann(ann: Any) -> bool:
    try:
        from .validation import Schema

        return inspect.isclass(ann) and issubclass(ann, Schema)
    except ImportError:
        return False


def _dep_kind(fn: Any) -> str:
    if inspect.isasyncgenfunction(fn):
        return "asyncgen"
    if inspect.isgeneratorfunction(fn):
        return "gen"
    if inspect.iscoroutinefunction(fn):
        return "async"
    return "sync"


def _hints_of(fn: Any) -> dict:
    """Resolved annotations for fn (handles `from __future__ import
    annotations` string form via the function's own globals).

    Falls back to raw signature annotations when anything is unresolvable —
    an unresolved annotation behaves exactly as before.
    """
    try:
        import typing

        hints = typing.get_type_hints(fn)
        if isinstance(hints, dict):
            return hints
    except Exception:
        pass
    try:
        return {
            name: p.annotation
            for name, p in inspect.signature(fn).parameters.items()
            if p.annotation is not inspect._empty
        }
    except (ValueError, TypeError):
        return {}


# ---- plan nodes ----


@dataclass
class DepSubParam:
    """One parameter of a dependency callable, pre-classified."""

    name: str
    kind: str  # request | nested | path | query | default | fallback
    annotation: Any = inspect._empty
    default: Any = None
    nested: DepNode | None = None  # only when kind == nested
    use_cache: bool = True


@dataclass
class DepNode:
    fn: Any = None
    use_cache: bool = True
    key: int = 0
    display: str = ""
    kind: str = "sync"  # primitive | sync | async | gen | asyncgen
    primitive_value: Any = None
    is_primitive: bool = False
    sub_params: list[DepSubParam] = field(default_factory=list)
    error: str | None = None  # set when Depends() needs a callable
    is_auth: bool = False
    auth_roles: tuple = ()
    auth_scheme: str = ""
    auth_header: str = ""
    auth_scopes: tuple = ()
    uses_config_secret: bool = False


@dataclass
class HandlerParam:
    name: str
    kind: str  # request | depends | background | schema | path | query | default | fallback
    annotation: Any = inspect._empty
    default: Any = None
    dep: DepNode | None = None
    use_cache: bool = True


@dataclass
class HandlerPlan:
    handler: Any
    handler_name: str = ""
    is_legacy_request_only: bool = False
    params: list[HandlerParam] = field(default_factory=list)
    has_body: bool = False
    has_background: bool = False
    compile_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    is_auth: bool = False
    auth_roles: tuple = ()
    auth_scheme: str = ""
    auth_header: str = ""
    auth_scopes: tuple = ()
    uses_config_secret: bool = False


def _auth_info(fn: Any) -> dict | None:
    """Explicit security markers (see auth.py). Duck-typed, no imports."""
    info = getattr(fn, "_ikarem_security", None)
    if isinstance(info, dict):
        return info
    scheme = getattr(type(fn), "_ikarem_security_scheme", None)
    if isinstance(scheme, str) and not inspect.isclass(fn):
        return {"scheme": scheme, "roles": ()}
    return None


def _compile_dep_node(fn: Any, use_cache: bool, stack: tuple[int, ...]) -> DepNode:
    from .di import Depends

    if isinstance(fn, Depends):
        return _compile_dep_node(fn.dep, fn.use_cache, stack)
    if fn is None:
        return DepNode(fn=None, use_cache=use_cache, error="Depends() needs a callable")
    if not callable(fn):
        return DepNode(
            fn=fn,
            use_cache=use_cache,
            key=id(fn),
            display=repr(fn),
            kind="primitive",
            primitive_value=fn,
            is_primitive=True,
        )
    key = id(fn)
    display = getattr(fn, "__name__", repr(fn))
    if key in stack:
        # Cycle found at compile time; stash as error so `ikarem check`
        # reports it and runtime raises the same ValueError as before.
        return DepNode(
            fn=fn,
            use_cache=use_cache,
            key=key,
            display=display,
            kind=_dep_kind(fn),
            error=f"Circular dependency detected involving '{display}'",
        )
    stack = stack + (key,)
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return DepNode(
            fn=fn,
            use_cache=use_cache,
            key=key,
            display=display,
            kind="sync",
            error=f"Cannot inspect dependency '{display}'",
        )
    node = DepNode(fn=fn, use_cache=use_cache, key=key, display=display, kind=_dep_kind(fn))
    auth = _auth_info(fn)
    if auth:
        node.is_auth = True
        node.auth_roles = tuple(auth.get("roles", ()))
        node.auth_scheme = str(auth.get("scheme", ""))
        node.auth_header = str(auth.get("header", "") or "")
        node.auth_scopes = tuple(auth.get("scopes", ()))
    if getattr(fn, "_ikarem_config_secret", False):
        node.uses_config_secret = True
    hints = _hints_of(fn)
    for pname, p in sig.parameters.items():
        ann, default = hints.get(pname, p.annotation), p.default
        if pname in ("request", "req"):
            node.sub_params.append(DepSubParam(pname, "request", ann, default))
        elif isinstance(default, Depends):
            if default.dep is None:
                node.sub_params.append(
                    DepSubParam(
                        pname,
                        "nested",
                        ann,
                        default,
                        nested=DepNode(fn=None, error=f"Depends() for '{pname}' needs a callable"),
                    )
                )
            else:
                node.sub_params.append(
                    DepSubParam(
                        pname,
                        "nested",
                        ann,
                        default,
                        nested=_compile_dep_node(default, default.use_cache, stack),
                        use_cache=default.use_cache,
                    )
                )
        elif pname in ("__ikarem_path_names__",):
            node.sub_params.append(DepSubParam(pname, "fallback", ann, default))
        else:
            # Defer path/query vs default decision to request time? No —
            # classify statically but keep runtime lookup order identical:
            # path_params > query > default > request-fallback.
            # We encode that as a single "dynamic" lookup to preserve exact
            # precedence without per-request signature work.
            node.sub_params.append(DepSubParam(pname, "dynamic", ann, default))
    # Auth propagates upward: a dep that (transitively) requires auth marks
    # every parent, so handlers + OpenAPI + MCP all see the same boundary.
    for sp in node.sub_params:
        child = sp.nested
        if child is None:
            continue
        if child.is_auth:
            node.is_auth = True
            if not node.auth_scheme:
                node.auth_scheme = child.auth_scheme
            if not node.auth_header:
                node.auth_header = child.auth_header
            node.auth_roles = tuple(dict.fromkeys((*node.auth_roles, *child.auth_roles)))
            node.auth_scopes = tuple(dict.fromkeys((*node.auth_scopes, *child.auth_scopes)))
        if child.uses_config_secret:
            node.uses_config_secret = True
    return node


def compile_handler(handler: Any) -> HandlerPlan:
    """Inspect handler once; return executable plan (never raises)."""
    from .di import Depends
    from .http import Request as Req

    name = getattr(handler, "__name__", repr(handler))
    plan = HandlerPlan(handler=handler, handler_name=name)
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        plan.compile_error = f"Cannot inspect handler '{name}'"
        return plan
    params = list(sig.parameters.values())
    hints = _hints_of(handler)
    if (
        len(params) == 1
        and params[0].default is inspect._empty
        and not isinstance(params[0].default, Depends)
    ):
        # Legacy: handler(request). Only when the single param actually wants
        # the request — not when it declares a body/path/query contract
        # (e.g. `async def h(item: Item)` or `async def h(uid: int)`).
        p0 = params[0]
        p0ann = hints.get(p0.name, p0.annotation)
        wants_request = (
            p0.name in ("request", "req") or p0ann is inspect._empty or p0ann is Any or p0ann is Req
        )
        if not wants_request:
            try:
                wants_request = inspect.isclass(p0ann) and issubclass(p0ann, Req)
            except Exception:
                pass
        if wants_request:
            plan.is_legacy_request_only = True
            return plan
    for p in params:
        pname, ann, default = p.name, hints.get(p.name, p.annotation), p.default
        if pname in ("request", "req") and (ann is inspect._empty or ann is Req or ann is Any):
            # Also accept subclasses of Request? Original checked
            # isinstance(request, Req) at runtime. Keep static fast path for
            # the common cases; runtime check below covers subclasses.
            plan.params.append(HandlerParam(pname, "request", ann, default))
            continue
        # Runtime Request-subclass check must come before Depends etc. only
        # for request-named params; handled at resolve time. Static: request
        # name with Request-subclass annotation.
        try:
            if pname in ("request", "req") and inspect.isclass(ann) and issubclass(ann, Req):
                plan.params.append(HandlerParam(pname, "request", ann, default))
                continue
        except Exception:
            pass
        if isinstance(default, Depends):
            dep_fn = default.dep or (ann if ann is not inspect._empty else None)
            if dep_fn is None or dep_fn is inspect._empty:
                plan.compile_error = f"Depends for '{pname}' needs a callable"
                plan.params.append(
                    HandlerParam(
                        pname,
                        "depends",
                        ann,
                        default,
                        dep=DepNode(fn=None, error=plan.compile_error),
                        use_cache=default.use_cache,
                    )
                )
                continue
            node = _compile_dep_node(dep_fn, default.use_cache, ())
            plan.params.append(
                HandlerParam(pname, "depends", ann, default, dep=node, use_cache=default.use_cache)
            )
            if node.error and node.error.startswith("Circular"):
                # Surface at handler level too for `ikarem check`.
                plan.warnings.append(f"param '{pname}': {node.error}")
            continue
        if ann is not inspect._empty and _is_background_ann(ann):
            plan.params.append(HandlerParam(pname, "background", ann, default))
            plan.has_background = True
            continue
        if ann is not inspect._empty and is_schema_like(ann):
            plan.params.append(HandlerParam(pname, "schema", ann, default))
            plan.has_body = True
            continue
        # path/query/default/fallback decided at runtime (request varies),
        # but statically warn when a param will silently get the whole Request.
        if default is not inspect._empty:
            # Could be path/query/default at runtime; mark dynamic.
            plan.params.append(HandlerParam(pname, "dynamic", ann, default))
        else:
            plan.params.append(HandlerParam(pname, "dynamic", ann, default))
            if ann is inspect._empty:
                plan.warnings.append(
                    f"param '{pname}' has no annotation/default — receives full Request at runtime"
                )
    for hp in plan.params:
        if hp.kind == "depends" and hp.dep is not None and hp.dep.is_auth:
            plan.is_auth = True
            if not plan.auth_scheme:
                plan.auth_scheme = hp.dep.auth_scheme
            if not plan.auth_header:
                plan.auth_header = hp.dep.auth_header
            plan.auth_roles = tuple(dict.fromkeys((*plan.auth_roles, *hp.dep.auth_roles)))
            plan.auth_scopes = tuple(dict.fromkeys((*plan.auth_scopes, *hp.dep.auth_scopes)))
        if hp.kind == "depends" and hp.dep is not None and hp.dep.uses_config_secret:
            plan.uses_config_secret = True
    return plan


# ---- plan cache (no repeated inspect, even for direct resolve calls) ----

_plan_cache: dict[int, HandlerPlan] = {}


def get_plan(handler: Any) -> HandlerPlan:
    key = id(handler)
    plan = _plan_cache.get(key)
    if plan is None or plan.handler is not handler:
        plan = compile_handler(handler)
        _plan_cache[key] = plan
    return plan


def invalidate_plan(handler: Any) -> None:
    _plan_cache.pop(id(handler), None)


# ---- execution (zero inspect.signature calls) ----


def _register_cleanup(request: Any, gen: Any) -> None:
    cleanups = getattr(request, "_dep_cleanups", None)
    if cleanups is None:
        request._dep_cleanups = cleanups = []
    cleanups.append(gen)


async def _resolve_node(node: DepNode, request: Any, cache: dict | None, stack: tuple[int, ...]) -> Any:
    if node.error:
        raise ValueError(node.error)
    if node.is_primitive:
        return node.primitive_value
    fn, key = node.fn, node.key
    if node.use_cache and cache is not None and key in cache:
        return cache[key]
    if key in stack:
        raise ValueError(f"Circular dependency detected involving '{node.display}'")
    stack = stack + (key,)
    kwargs: dict[str, Any] = {}
    path_params = getattr(request, "path_params", {})
    query = getattr(request, "query", {})
    for sp in node.sub_params:
        if sp.kind == "request":
            kwargs[sp.name] = request
        elif sp.kind == "nested":
            assert sp.nested is not None
            child_cache = cache if (cache is not None and sp.use_cache) else None
            # Thread parent cache-poisoning: if this node's cache is None,
            # children are uncached too (matches legacy _call threading).
            kwargs[sp.name] = await _resolve_node(sp.nested, request, child_cache, stack)
        else:  # dynamic: path > query > default > request (legacy _dep_kwargs order)
            if sp.name in path_params:
                kwargs[sp.name] = path_params[sp.name]
            elif sp.name in query:
                kwargs[sp.name] = _coerce_query(query[sp.name], sp.annotation)
            elif sp.default is not inspect._empty:
                kwargs[sp.name] = sp.default
            else:
                kwargs[sp.name] = request
    if node.kind == "asyncgen":
        agen = fn(**kwargs)
        try:
            value = await agen.__anext__()
        except StopAsyncIteration:
            raise RuntimeError(f"async generator dependency '{node.display}' yielded nothing")
        _register_cleanup(request, agen)
        result = value
    elif node.kind == "gen":
        gen = fn(**kwargs)
        try:
            result = next(gen)
        except StopIteration:
            raise RuntimeError(f"generator dependency '{node.display}' yielded nothing")
        _register_cleanup(request, gen)
    elif node.kind == "async":
        result = await fn(**kwargs)
    else:
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
    if node.use_cache and cache is not None:
        cache[key] = result
    return result


async def resolve_compiled(handler: Any, request: Any) -> Any:
    """Drop-in replacement for di.resolve_handler with precompiled plans."""
    import inspect as _inspect

    plan = get_plan(handler)
    if plan.compile_error and not plan.params:
        raise ValueError(plan.compile_error)
    if plan.is_legacy_request_only:
        r = handler(request)
        return await r if _inspect.isawaitable(r) else r
    from .http import Request as Req

    cache: dict = {}
    kwargs: dict[str, Any] = {}
    body_json: Any = _MISSING
    for hp in plan.params:
        if hp.kind == "request":
            kwargs[hp.name] = request
            continue
        if hp.kind == "depends":
            assert hp.dep is not None
            if hp.dep.error and hp.dep.fn is None:
                raise ValueError(hp.dep.error)
            sub_cache = cache if hp.use_cache else None
            kwargs[hp.name] = await _resolve_node(hp.dep, request, sub_cache, ())
            continue
        if hp.kind == "background":
            from .background import BackgroundTasks

            bt = getattr(request, "_bg", None)
            if bt is None:
                bt = BackgroundTasks()
                request._bg = bt
            kwargs[hp.name] = bt
            continue
        if hp.kind == "schema":
            if body_json is _MISSING:
                from .errors import HTTPException as _HTTPException

                try:
                    body_json = await request.json()
                except _HTTPException:
                    # A 413/400 signal is a verdict, not a parse miss: re-raise
                    # instead of morphing oversized bodies into form parsing
                    # (form() would re-read an already-consumed body).
                    raise
                except Exception:
                    # HTML forms validate too: first values as the object.
                    try:
                        from .http import UploadFile as _UF

                        form = await request.form()
                        body_json = {k: v for k, v in form.items() if not isinstance(v, _UF)}
                    except Exception:
                        body_json = {}
            try:
                kwargs[hp.name] = validate_schema(hp.annotation, body_json or {})
            except Exception as e:
                from .errors import BadRequest

                raise BadRequest(f"validation failed: {e}")
            continue
        # dynamic: path > query > default > request-fallback (legacy order).
        # Request-named params with Request annotations were handled above;
        # a request-named param with exotic annotation still gets request here
        # only if no path/query/default shadowed it — check runtime type guard
        # to match legacy `isinstance(request, Req)` behavior.
        if hp.name in ("request", "req") and isinstance(request, Req):
            if hp.annotation is _inspect._empty or hp.annotation is Req or hp.annotation is Any:
                kwargs[hp.name] = request
                continue
            try:
                if _inspect.isclass(hp.annotation) and isinstance(request, hp.annotation):
                    kwargs[hp.name] = request
                    continue
            except Exception:
                pass
        path_params = getattr(request, "path_params", {})
        if hp.name in path_params:
            kwargs[hp.name] = path_params[hp.name]
            continue
        query = getattr(request, "query", {})
        if hp.name in query:
            kwargs[hp.name] = _coerce_query(query[hp.name], hp.annotation)
            continue
        if hp.default is not _inspect._empty:
            kwargs[hp.name] = hp.default
            continue
        kwargs[hp.name] = request
    result = handler(**kwargs)
    if _inspect.isawaitable(result):
        result = await result
    return result


# ---- shared route description: one IR for OpenAPI + MCP + check ----

_ROUTE_PARAM_RE = _re.compile(r"\{(\w+)(?::(\w+))?\}")


def _json_type(ann: Any) -> dict:
    try:
        from .validation import _js_type

        t = _js_type(ann)
        return t if isinstance(t, dict) and t else {"type": "string"}
    except Exception:
        if ann is int:
            return {"type": "integer"}
        if ann is float:
            return {"type": "number"}
        if ann is bool:
            return {"type": "boolean"}
        return {"type": "string"}


def _is_optional_ann(ann: Any) -> bool:
    try:
        from typing import Union, get_args, get_origin

        return get_origin(ann) is Union and type(None) in get_args(ann)
    except Exception:
        return False


@dataclass
class QueryField:
    name: str
    schema: dict = field(default_factory=dict)
    required: bool = False
    default: Any = None


@dataclass
class RouteDescription:
    path: str
    methods: list[str] = field(default_factory=list)
    handler_name: str = ""
    doc: str = ""
    path_params: list[str] = field(default_factory=list)
    path_converters: dict = field(default_factory=dict)
    query: list[QueryField] = field(default_factory=list)
    body_cls: Any = None
    body_schema: dict = field(default_factory=dict)
    is_auth: bool = False
    auth_roles: tuple = ()
    auth_scheme: str = ""
    auth_header: str = ""
    auth_scopes: tuple = ()


def describe_route(route: Any) -> RouteDescription:
    """Static HTTP contract for one route, derived from its compiled plan."""
    import inspect as _inspect

    plan = get_plan(route.handler)
    desc = RouteDescription(
        path=route.path,
        methods=sorted(route.methods),
        handler_name=plan.handler_name,
        doc=(_inspect.getdoc(route.handler) or "").strip(),
        is_auth=plan.is_auth,
        auth_roles=plan.auth_roles,
        auth_scheme=plan.auth_scheme,
        auth_header=plan.auth_header,
        auth_scopes=plan.auth_scopes,
    )
    for m in _ROUTE_PARAM_RE.finditer(route.path):
        desc.path_params.append(m.group(1))
        desc.path_converters[m.group(1)] = m.group(2) or "str"
    path_names = set(desc.path_params)
    fields: dict[str, QueryField] = {}

    def _add(name: str, ann: Any, default: Any) -> None:
        if name in path_names or name in ("request", "req"):
            return
        if name in fields:
            return
        if _inspect.isclass(ann) and ann is not _inspect._empty:
            try:
                from .http import Request as Req

                if issubclass(ann, Req):
                    return
            except Exception:
                pass
        required = default is _inspect._empty and not _is_optional_ann(ann) and ann is not _inspect._empty
        fields[name] = QueryField(
            name=name,
            schema=_json_type(None if ann is _inspect._empty else ann),
            required=required,
            default=None if default is _inspect._empty else default,
        )

    def _walk_dep(node: DepNode | None, seen: set[int]) -> None:
        if node is None or id(node) in seen or node.is_primitive:
            return
        seen.add(id(node))
        for sp in node.sub_params:
            if sp.kind == "nested":
                _walk_dep(sp.nested, seen)
            elif sp.kind == "dynamic":
                _add(sp.name, sp.annotation, sp.default)

    if not plan.is_legacy_request_only:
        for hp in plan.params:
            if hp.kind == "dynamic":
                _add(hp.name, hp.annotation, hp.default)
            elif hp.kind == "depends" and hp.dep is not None:
                _walk_dep(hp.dep, set())
            elif hp.kind == "schema" and desc.body_cls is None:
                desc.body_cls = hp.annotation
                try:
                    desc.body_schema = schema_json_schema(hp.annotation)
                except Exception:
                    desc.body_schema = {"type": "object"}
    desc.query = list(fields.values())
    return desc


def describe_app(app: Any) -> dict:
    """Token-efficient whole-app manifest for LLM context (inspect/MCP).

    One entry per route+method; nulls omitted. An agent can plan every
    call from this alone — path/query/body shapes plus auth boundaries.
    """
    from types import SimpleNamespace

    router = getattr(app, "router", None)
    routes = getattr(router, "routes", [])
    out: list[dict] = []
    for route in routes:
        if getattr(route.handler, "_ikarem_internal", False):
            continue
        plan = get_plan(route.handler)
        if plan.compile_error:
            continue
        for method in sorted(route.methods):
            single = SimpleNamespace(
                path=route.path, methods={method}, handler=route.handler, name=route.name
            )
            desc = describe_route(single)
            entry: dict[str, Any] = {
                "method": method,
                "path": route.path,
                "handler": plan.handler_name,
            }
            if desc.doc:
                entry["summary"] = desc.doc.split("\n")[0][:140]
            if desc.path_params:
                entry["path_params"] = [
                    {"name": n, "type": _conv_type(desc.path_converters.get(n))} for n in desc.path_params
                ]
            if desc.query:
                entry["query"] = [
                    {"name": q.name, "type": (q.schema or {}).get("type", "string"), "required": q.required}
                    for q in desc.query
                ]
            if desc.body_schema:
                entry["body"] = desc.body_schema
            if desc.is_auth:
                auth: dict[str, Any] = {"scheme": desc.auth_scheme or "bearer"}
                if desc.auth_roles:
                    auth["roles"] = list(desc.auth_roles)
                if desc.auth_scopes:
                    auth["scopes"] = list(desc.auth_scopes)
                if desc.auth_header:
                    auth["header"] = desc.auth_header
                entry["auth"] = auth
            out.append(entry)
    return {"routes": out, "count": len(out)}


def _conv_type(converter: str | None) -> str:
    return {"int": "integer", "float": "number", "uuid": "string", "path": "string"}.get(
        converter or "str", "string"
    )


def check_app(app: Any) -> dict:
    """Statically compile every route; return {errors, warnings, routes}."""
    report: dict[str, Any] = {"errors": [], "warnings": [], "routes": []}
    router = getattr(app, "router", None)
    routes = getattr(router, "routes", [])
    seen_routes: dict[tuple, str] = {}
    for r in routes:
        if getattr(r.handler, "_ikarem_internal", False):
            continue
        for method in sorted(r.methods):
            key = (method, r.path)
            handler_name = getattr(r.handler, "__name__", repr(r.handler))
            if key in seen_routes:
                report["errors"].append(
                    f"{method} {r.path}: duplicate route (handlers "
                    f"'{seen_routes[key]}' and '{handler_name}'); the first wins, "
                    f"the second never runs"
                )
            else:
                seen_routes[key] = handler_name
        plan = get_plan(r.handler)
        entry = {"path": r.path, "methods": sorted(r.methods), "handler": plan.handler_name}
        report["routes"].append(entry)
        if plan.compile_error:
            report["errors"].append(f"{r.path} [{plan.handler_name}]: {plan.compile_error}")
        for w in plan.warnings:
            report["warnings"].append(f"{r.path} [{plan.handler_name}]: {w}")

        seen_errs = set(report["errors"])

        def _walk(node: DepNode | None, seen: set[int]) -> None:
            if node is None or id(node) in seen:
                return
            seen.add(id(node))
            if node.error:
                msg = f"{r.path} [{plan.handler_name}]: dep '{node.display}': {node.error}"
                if msg not in seen_errs and node.error not in (plan.compile_error or ""):
                    report["errors"].append(msg)
                    seen_errs.add(msg)
                return
            for sp in node.sub_params:
                if sp.kind == "nested" and sp.nested is not None:
                    _walk(sp.nested, seen)

        for hp in plan.params:
            if hp.kind == "depends" and hp.dep is not None:
                _walk(hp.dep, set())
        # Exposure audit (deny-by-audit): frameworks default every route to
        # public and never mention it — forgotten auth ships silently. Any
        # mutating route without a token/API-key guard is one warning, never
        # an error: register/login are public on purpose, session-cookie
        # routes carry their guard outside the DI graph. The warning exists
        # so every public write is a conscious choice, visible in review.
        unsafe = sorted(m for m in r.methods if m.upper() not in ("GET", "HEAD", "OPTIONS"))
        if unsafe and not plan.compile_error and not plan.is_auth:
            report["warnings"].append(
                f"{r.path} [{plan.handler_name}]: {','.join(unsafe)} with no bearer/API-key guard — "
                "public by default. If intentionally public (register/login) or session-cookie "
                "protected, leave it; otherwise add claims=Depends(require_roles(...)) or APIKeyAuth."
            )
        from .audit import audit_handler

        for finding in audit_handler(r.handler):
            report["warnings"].append(f"{r.path} [{plan.handler_name}]: {finding}")
    return report
