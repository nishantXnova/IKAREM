"""ASGI middleware bridge: run outside middleware inside IKAREM. Reference adapter.

Consume the entire Starlette/FastAPI middleware ecosystem without rewriting it::

    from adapters.asgi_bridge import ASGIMiddlewareAdapter

    app.use(ASGIMiddlewareAdapter(lambda asgi_app: CORSMiddleware(asgi_app, ...)))

One direction only (outside→inside): the donor wraps a downstream that runs
the native pipeline, so adapted traffic gets compiled plans, IKAREM error
rendering, and every middleware after this one in the stack. The donor never
touches the core, and this module never touches ``ikarem/`` privates — only
public ``Request``/``Response``/``Middleware`` API.

The ONE thing it does better than the default (running that middleware on
Starlette itself): the wrapped route stays an IKAREM route — ``ikarem check``
audits it, ``describe_app``/MCP exposes it as a tool, and native shields
(SpikeManager, rate limits, timeouts) can sit in front of it in the stack.

Removal path: replace each adapted middleware with its native IKAREM
equivalent (``CORSMiddleware`` → ``ikarem.security.CORSMiddleware``,
trusted-host → ``TrustedHostMiddleware``, etc.) and delete the ``app.use``
line. No handler changes needed — handlers never see the bridge.

Honest limitations (the adapter is second-best by design):
- The request body is buffered once per request through the bridge
  (``await req.body()``); the handler reuses the cache, so bodies stay
  correct, but put native middleware first for huge-upload routes.
- Donor scope mutations hit a copy — they never leak into IKAREM routing.
  Request-header changes by the donor are likewise not reflected on
  ``req.headers`` (parsed at request creation); response-header changes
  pass through untouched.
- Chunked donor responses are buffered into one IKAREM ``Response``.
- Donor exceptions propagate into the native pipeline and render as the
  standard 500 JSON — same as a native middleware raising.
"""

from __future__ import annotations

import contextvars
from typing import Any

from ikarem.http import Response
from ikarem.middleware import Middleware

__all__ = ["ASGIMiddlewareAdapter"]

_current: contextvars.ContextVar[Any] = contextvars.ContextVar("ikarem_asgi_bridge")


class _ReplayReceive:
    """One-shot body replay for the donor (seeded from the public body cache)."""

    def __init__(self, body: bytes):
        self._msgs = [{"type": "http.request", "body": body, "more_body": False}]
        self._pos = 0

    async def __call__(self) -> dict:
        if self._pos < len(self._msgs):
            msg = self._msgs[self._pos]
            self._pos += 1
            return msg
        return {"type": "http.disconnect"}


class _CaptureSend:
    """Collect the donor's response messages back into one IKAREM response."""

    def __init__(self):
        self.status = 200
        self.raw_headers: list[tuple[bytes, bytes]] = []
        self.chunks: list[bytes] = []
        self.started = False

    async def __call__(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "http.response.start":
            self.started = True
            self.status = int(msg.get("status", 200))
            self.raw_headers = list(msg.get("headers", []))
        elif kind == "http.response.body":
            body = msg.get("body", b"")
            if isinstance(body, str):
                body = body.encode()
            self.chunks.append(body)

    def to_response(self) -> Response:
        headers: dict[str, Any] = {}
        media_type = "application/octet-stream"
        for k, v in self.raw_headers:
            name, val = k.decode().lower(), v.decode()
            if name == "content-type":
                media_type = val
                continue
            if name == "content-length":
                continue  # IKAREM recomputes it from the buffered body
            if name in headers:
                prev = headers[name]
                headers[name] = [*prev, val] if isinstance(prev, list) else [prev, val]
            else:
                headers[name] = val
        return Response(b"".join(self.chunks), self.status, headers, media_type)


class ASGIMiddlewareAdapter(Middleware):
    """Wrap a pure-ASGI middleware factory as IKAREM middleware.

    ``factory`` takes the downstream ASGI app and returns the wrapped app —
    the Starlette shape (``lambda app: CORSMiddleware(app, ...)``). Applied
    once at registration; per-request work is one body buffer plus message
    capture, no reflection.
    """

    def __init__(self, factory: Any):
        if not callable(factory):
            raise ValueError(
                "ASGIMiddlewareAdapter needs a middleware factory taking the downstream "
                "ASGI app, e.g. ASGIMiddlewareAdapter(lambda app: CORSMiddleware(app, ...))"
            )

        async def downstream(scope: dict, receive: Any, send: Any) -> None:
            state = _current.get(None)
            if state is None:
                raise RuntimeError(
                    "ASGI bridge downstream reached outside a request (report this as an IKAREM bug)"
                )
            req, call_next = state
            resp = await call_next(req)
            await resp(scope, receive, send)

        try:
            wrapped = factory(downstream)
        except Exception as e:
            raise ValueError(
                f"ASGI middleware factory failed at registration: {e}. Pass a factory "
                "taking the downstream ASGI app."
            ) from e
        if not callable(wrapped):
            raise ValueError(
                "ASGI middleware factory must return an ASGI app (an async "
                "callable taking scope/receive/send); got "
                f"{type(wrapped).__name__}. Pass e.g. lambda app: CORSMiddleware(app, ...)"
            )
        self._wrapped = wrapped

    async def __call__(self, req: Any, call_next: Any) -> Any:
        body = await req.body()  # buffered once; the handler reuses the cache
        scope = dict(req.scope)
        scope["type"] = "http"
        send = _CaptureSend()
        token = _current.set((req, call_next))
        try:
            await self._wrapped(scope, _ReplayReceive(body), send)
        finally:
            _current.reset(token)
        if not send.started:
            raise RuntimeError(
                "adapted ASGI middleware never sent http.response.start — "
                "it must call the downstream app or send a response itself"
            )
        return send.to_response()
