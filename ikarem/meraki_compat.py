"""Meraki drop-in replacement, powered by IKAREM. Zero switching cost.

A Meraki app runs UNCHANGED on IKAREM::

    # from meraki import Meraki                      # before
    from ikarem.meraki_compat import Meraki          # after — nothing else changes
    from meraki.core.response import Response        # still works: re-exported below

Faithful to Meraki's observable behavior: static routes, 404/405 plain-text
bodies, byte-pair headers, ``query_params`` tuples, ``add_middleware`` onion,
``Response(body, status_code, headers)`` with ``.send()``.

Escape hatches (opt-in, per handler): ``request.ikarem`` is the full IKAREM
request (``await body()/json()``, cookies, path params), and handlers may
return IKAREM responses / dicts / Schema models directly.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Callable


class MerakiRequest:
    """Meraki-shaped view over an IKAREM request."""

    def __init__(self, ikarem_request: Any):
        self.ikarem = ikarem_request
        self.method: str = ikarem_request.method
        self.path: str = ikarem_request.path
        scope = getattr(ikarem_request, "scope", {})
        self.headers: list[tuple[bytes, bytes]] = list(scope.get("headers", []))
        qs = scope.get("query_string", b"").decode("latin-1")
        self.query_params: list[tuple[str, str]] = urllib.parse.parse_qsl(qs)


class MerakiResponse:
    """Meraki-shaped response: bytes body, .send(), ASGI-callable."""

    def __init__(
        self, body: bytes = b"", status_code: int = 200, headers: list[tuple[bytes, bytes]] | None = None
    ):
        self.body = body
        self.status_code = status_code
        if headers is None:
            self.headers: list[tuple[bytes, bytes]] = [
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ]
        else:
            self.headers = headers

    async def send(self, send_callable: Any) -> None:
        await send_callable(
            {"type": "http.response.start", "status": self.status_code, "headers": self.headers}
        )
        await send_callable({"type": "http.response.body", "body": getattr(self, "body", b"")})

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self.send(send)


def _to_ikarem_response(value: Any) -> Any:
    from .http import Response as IkaremResponse
    from .http import to_response

    if isinstance(value, IkaremResponse):
        return value
    if isinstance(value, MerakiResponse):
        # content-length is recomputed by IKAREM at send time — carrying the
        # original would emit a stale duplicate.
        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in value.headers
            if k.lower() != b"content-length"
        }
        return IkaremResponse(value.body, status_code=value.status_code, headers=headers)
    return to_response(value)


class Meraki:
    """``from meraki import Meraki`` — same surface, IKAREM engine."""

    def __init__(self, debug: bool = False, **config: Any):
        from .app import Ikarem
        from .errors import MethodNotAllowed, NotFound
        from .http import TextResponse

        self._app = Ikarem(debug=debug, enable_docs=False, **config)

        @self._app.exception_handler(NotFound)
        async def _nf(req: Any, exc: Any) -> Any:
            return TextResponse("Not Found", status_code=404)

        @self._app.exception_handler(MethodNotAllowed)
        async def _mna(req: Any, exc: Any) -> Any:
            return TextResponse("Method Not Allowed", status_code=405)

    # ---- routing (Meraki: static paths only — IKAREM also compiles params) ----
    def _register(self, path: str, methods: list[str]) -> Callable:
        def deco(handler: Callable) -> Callable:
            async def _wrapped(ik_req: Any) -> Any:
                result = handler(MerakiRequest(ik_req))
                if hasattr(result, "__await__"):
                    result = await result
                return _to_ikarem_response(result)

            _wrapped.__name__ = getattr(handler, "__name__", "handler")
            self._app.router.add(path, set(methods), _wrapped)
            return handler

        return deco

    def get(self, path: str) -> Callable:
        return self._register(path, ["GET"])

    def post(self, path: str) -> Callable:
        return self._register(path, ["POST"])

    def put(self, path: str) -> Callable:
        return self._register(path, ["PUT"])

    def delete(self, path: str) -> Callable:
        return self._register(path, ["DELETE"])

    def patch(self, path: str) -> Callable:
        return self._register(path, ["PATCH"])

    # ---- middleware: same (request, call_next) onion ----
    def add_middleware(self, middleware: Callable) -> None:
        async def _adapted(ik_req: Any, call_next: Any) -> Any:
            async def _next(inner: Any = None) -> Any:
                req = inner if inner is not None else ik_req
                if isinstance(req, MerakiRequest):
                    res = await call_next(req.ikarem)
                else:
                    res = await call_next(req)
                return res

            result = await middleware(MerakiRequest(ik_req), _next)
            return _to_ikarem_response(result)

        self._app.use(_adapted)

    @property
    def ikarem(self) -> Any:
        """The engine underneath: routers, plugins, config, check(), mcp()."""
        return self._app

    async def startup(self) -> None:
        await self._app.startup()

    async def shutdown(self) -> None:
        await self._app.shutdown()

    def run(self, *a: Any, **k: Any) -> None:
        self._app.run(*a, **k)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self._app(scope, receive, send)
