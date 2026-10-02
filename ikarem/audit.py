"""Static source audit: what the interpreter accepts but production regrets.

Runs inside ``check`` (never per-request): each route handler's source is
AST-scanned for two precise patterns — SQL glued with f-strings /
``.format()`` / ``%`` in db calls (injection the test suite won't catch),
and blocking calls (``time.sleep``, ``requests.*``) on the event loop.

Findings are warnings with remedies, never errors: table names can't use
``?`` placeholders, so failing the gate would punish allowlisted
identifiers alongside real bugs. A warning that is right 99% of the time
beats an error that teaches devs to ignore the tool.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

_DB_METHODS = frozenset({"execute", "execute_many", "fetch_one", "fetch_all"})


def _handler_functions(handler: Any) -> list[tuple[str, Any]]:
    """Unwrap to auditable functions: Blueprint wrappers to the original,
    MethodView dispatchers to each verb method."""
    try:
        target = inspect.unwrap(handler)
    except Exception:
        return []
    view_cls = getattr(target, "__view_class__", None)
    if view_cls is not None:
        out = []
        for verb in ("get", "post", "put", "patch", "delete"):
            fn = getattr(view_cls, verb, None)
            if callable(fn):
                out.append((verb, fn))
        return out
    return [("", target)]


def _source_of(fn: Any) -> str | None:
    try:
        return textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return None


def _is_fstring_with_fields(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) and any(isinstance(v, ast.FormattedValue) for v in node.values)


def _is_format_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and bool(node.args or node.keywords)
    )


def _is_percent_format(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)


def _query_danger(arg: ast.AST) -> str | None:
    """How the query string is interpolated, or None when parameter-safe."""
    if _is_fstring_with_fields(arg):
        return "f-string"
    if _is_format_call(arg):
        return ".format()"
    if _is_percent_format(arg):
        return "%-formatting"
    return None


def _blocking_call(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "time" and func.attr == "sleep":
            return "time.sleep"
        if func.value.id == "requests":
            return f"requests.{func.attr}"
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _DB_METHODS and node.args:
            danger = _query_danger(node.args[0])
            if danger is not None:
                self.findings.append(
                    f"SQL built with {danger} in db.{func.attr}(...) — the interpreter accepts it "
                    "and tests pass on benign data; injection arrives in production. Use ? placeholders "
                    "for values (allowlist validated table/column names instead of interpolating)."
                )
        blocking = _blocking_call(node)
        if blocking is not None:
            self.findings.append(
                f"blocking {blocking}(...) on the event loop stalls every request on the worker — "
                "await asyncio.to_thread(...) or switch to an async driver (guide ch15)."
            )
        self.generic_visit(node)


def audit_handler(handler: Any) -> list[str]:
    """Audit one route handler's source. Returns warning strings (possibly
    empty). Never raises: unauditable handlers (lambdas, REPL, C code)
    simply yield nothing."""
    out: list[str] = []
    for label, fn in _handler_functions(handler):
        src = _source_of(fn)
        if src is None:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        visitor = _Visitor()
        visitor.visit(tree)
        prefix = f"method {label}: " if label else ""
        out.extend(prefix + finding for finding in visitor.findings)
    return out


def audit_app(app: Any) -> dict[str, list[str]]:
    """Audit every route: {path: [findings]} for routes with findings."""
    result: dict[str, list[str]] = {}
    for route in getattr(getattr(app, "router", None), "routes", []):
        if getattr(route.handler, "_ikarem_internal", False):
            continue
        findings = audit_handler(route.handler)
        if findings:
            result.setdefault(route.path, []).extend(findings)
    return result
