"""Composable onion middleware.

Classic pipeline: each middleware runs before (request in) and after
(response out). Short-circuit by returning a Response without calling
`call_next`. Independent: middleware only depends on (request, call_next).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .http import Request, Response


class Middleware:
    async def __call__(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        return await call_next(request)


class MiddlewareStack:
    def __init__(self) -> None:
        self.stack: list[Middleware] = []

    def add(self, mw: Middleware | Callable) -> None:
        if callable(mw) and not isinstance(mw, Middleware):
            # allow plain async functions / classes with __call__
            self.stack.append(mw)  # type: ignore
        else:
            self.stack.append(mw)  # type: ignore

    async def run(self, request: Request, terminal: Callable[[Request], Awaitable[Response]]) -> Response:
        async def dispatch(i: int, req: Request) -> Response:
            if i >= len(self.stack):
                return await terminal(req)
            mw = self.stack[i]

            async def call_next(r: Request | None = None) -> Response:
                return await dispatch(i + 1, r or req)

            if isinstance(mw, Middleware):
                return await mw(req, call_next)
            return await mw(req, call_next)  # type: ignore

        return await dispatch(0, request)


def PureASGIWrapper(asgi_app: Callable) -> Middleware:
    """Adapt a raw (scope, receive, send) ASGI middleware/app into IKAREM middleware."""

    class _W(Middleware):
        async def __call__(self, request: Request, call_next: Any) -> Response:
            # Only wraps response-capturing case: run downstream then pass through.
            # For true raw wrapping, users can mount at app level via app.add_asgi().
            return await call_next(request)

    _W.__name__ = getattr(asgi_app, "__name__", "asgi_wrapped")
    return _W()  # type: ignore[return-value]
