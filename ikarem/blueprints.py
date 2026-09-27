"""Blueprints: Flask-style modular route groups, compiled like everything else.

A Blueprint collects routes + hooks and is mounted on an app with an
optional URL prefix. Unlike copy-paste routers, blueprints carry their own
before/after-request hooks and error handlers, and their routes are named
``blueprint.route`` so ``url_for`` stays unambiguous.

Hooks wrap each handler ONCE at registration, so the compiled DI plan sees
a plain ``(req)`` callable — zero per-request overhead versus app routes.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable


class Blueprint:
    def __init__(self, name: str, url_prefix: str = ""):
        self.name = name
        self.url_prefix = url_prefix.rstrip("/")
        self._routes: list[tuple[str, set[str], Callable, str | None]] = []
        self._before: list[Callable] = []
        self._after: list[Callable] = []
        self._errors: dict[type, Callable] = {}

    # ---- route sugar (mirrors Ikarem) ----
    def route(self, path: str, methods: list[str]):
        def deco(fn: Callable) -> Callable:
            self._routes.append((path, set(methods), fn, None))
            return fn

        return deco

    def get(self, path: str):
        return self.route(path, ["GET"])

    def post(self, path: str):
        return self.route(path, ["POST"])

    def put(self, path: str):
        return self.route(path, ["PUT"])

    def patch(self, path: str):
        return self.route(path, ["PATCH"])

    def delete(self, path: str):
        return self.route(path, ["DELETE"])

    # ---- hooks ----
    def before_request(self, fn: Callable) -> Callable:
        """Return None to continue, or a Response/dict to short-circuit."""
        self._before.append(fn)
        return fn

    def after_request(self, fn: Callable) -> Callable:
        """Receives (req, response); return the (possibly new) response."""
        self._after.append(fn)
        return fn

    def errorhandler(self, exc_type: type[Exception]):
        def deco(fn: Callable) -> Callable:
            self._errors[exc_type] = fn
            return fn

        return deco

    def _find_error(self, exc: Exception) -> Callable | None:
        for klass in type(exc).__mro__:
            if klass in self._errors:
                return self._errors[klass]
        return None

    def _wrap(self, handler: Callable) -> Callable:
        from .di import resolve_handler
        from .errors import HTTPException
        from .http import to_response

        befores, afters, find_error = self._before, self._after, self._find_error

        @functools.wraps(handler)
        async def _w(request: Any) -> Any:
            for f in befores:
                r = f(request)
                if inspect.isawaitable(r):
                    r = await r
                if r is not None:
                    resp = to_response(r)
                    break
            else:
                try:
                    resp = to_response(await resolve_handler(handler, request))
                except HTTPException as e:
                    h = find_error(e)
                    if h is None:
                        raise
                    hr = h(request, e)
                    if inspect.isawaitable(hr):
                        hr = await hr
                    resp = to_response(hr)
            for f in afters:
                r = f(request, resp)
                if inspect.isawaitable(r):
                    r = await r
                if r is not None:
                    resp = r
            return resp

        return _w
