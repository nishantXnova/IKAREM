"""Compiled path routing with converters.

Supported syntax:
  /users/{uid}            -> str
  /users/{uid:int}        -> int (rejects non-int with no-match)
  /files/{p:path}         -> str incl. slashes
  /x/{f:float}, /o/{i:uuid}

Beats naive string matching: each route pre-compiles to a regex once at
registration; matching distinguishes 404 (no path matches) from 405
(path matches, method doesn't) like a real framework should.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable

_CONVERTERS = {
    "str": (r"[^/]+", str),
    "int": (r"[0-9]+", int),
    "float": (r"[0-9]+(?:\.[0-9]+)?", float),
    "uuid": (r"[0-9a-fA-F-]{36}", lambda s: uuid.UUID(s)),
    "path": (r".+", str),
}

_PARAM_RE = re.compile(r"\{(\w+)(?::(\w+))?\}")


class Route:
    def __init__(self, path: str, methods: set[str], handler: Callable, name: str | None = None):
        self.path = path
        self.methods = {m.upper() for m in methods}
        self.handler = handler
        self.name = name or getattr(handler, "__name__", path)
        self.regex, self.converters = self._compile(path)

    def _compile(self, path: str) -> tuple[re.Pattern, dict[str, Callable]]:
        converters: dict[str, Callable] = {}
        i = 0
        pattern = ""
        for m in _PARAM_RE.finditer(path):
            pattern += re.escape(path[i : m.start()])
            pname, cname = m.group(1), (m.group(2) or "str")
            if cname not in _CONVERTERS:
                raise ValueError(f"Unknown converter '{cname}' in route '{path}'")
            rx, cast = _CONVERTERS[cname]
            pattern += f"(?P<{pname}>{rx})"
            converters[pname] = cast
            i = m.end()
        pattern += re.escape(path[i:])
        return re.compile(f"^{pattern}$"), converters

    def match(self, path: str) -> dict[str, Any] | None:
        m = self.regex.match(path)
        if not m:
            return None
        out: dict[str, Any] = {}
        for k, v in m.groupdict().items():
            try:
                out[k] = self.converters[k](v)
            except Exception:
                return None
        return out


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []
        self._static: dict[tuple[str, str], Route] = {}
        self._has_params = False

    def add(
        self, path: str, methods: set[str] | list[str], handler: Callable, name: str | None = None
    ) -> Route:
        route = Route(path, set(methods), handler, name)
        self.routes.append(route)
        if "{" in path:
            self._has_params = True
        else:
            for m in route.methods:
                self._static[(m, path)] = route
        return route

    def route(self, path: str, methods: list[str]):
        def deco(fn: Callable) -> Callable:
            self.add(path, set(methods), fn)
            return fn

        return deco

    def match(self, method: str, path: str) -> tuple[Callable, dict[str, Any]]:
        """Returns (handler, path_params) or raises NotFound/MethodNotAllowed."""
        from .errors import MethodNotAllowed, NotFound

        if not self._has_params:
            # All-static table (the common case): O(1) dict hit, no regex.
            # Falls through to the full scan so 404/405 stay exact.
            route = self._static.get((method.upper(), path))
            if route is not None:
                return route.handler, {}
        path_matched = False
        allowed: set[str] = set()
        for r in self.routes:
            params = r.match(path)
            if params is None:
                continue
            path_matched = True
            allowed |= r.methods
            if method.upper() in r.methods:
                return r.handler, params
        if path_matched:
            raise MethodNotAllowed(f"Method {method} not allowed. Allow: {sorted(allowed)}")
        raise NotFound(f"No route for {method} {path}")

    def url_for(self, name: str, **params: Any) -> str:
        for r in self.routes:
            if r.name == name:
                url = r.path
                for k, v in params.items():
                    url = re.sub(r"\{" + k + r"(?::\w+)?\}", str(v), url)
                return url
        raise KeyError(f"No route named '{name}'")
