"""Conditional requests: ETag validation for bandwidth-free repolls.

Any response carrying an ``ETag`` (``NISHResponse`` sets a content-hash
one by default) answers 304 with no body when the client already holds
it — ``If-None-Match`` matched, bytes saved, handlers untouched::

    app.use(ConditionalMiddleware())

Generic: works for ETags your own handlers set too, not just NISH.
"""

from __future__ import annotations

from typing import Any

from .middleware import Middleware


def _etag_match(response_etag: str, header: str) -> bool:
    """Strong comparison per RFC 9110 (plus a tolerant strip of W(/quotes)."""
    clean = response_etag.strip()
    for token in header.split(","):
        token = token.strip()
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:].strip()
        if clean == token:
            return True
    return False


class ConditionalMiddleware(Middleware):
    """GET/HEAD with a matching ``If-None-Match`` short-circuits to 304.

    The 304 keeps the ETag so clients can keep validating. Only acts when
    the downstream response actually carries an ETag — otherwise invisible.
    """

    async def __call__(self, req: Any, call_next: Any) -> Any:
        from .http import Response

        resp = await call_next(req)
        if req.method not in ("GET", "HEAD"):
            return resp
        etag = None
        for k, v in resp.headers.items():
            if k.lower() == "etag":
                etag = v
                break
        if not etag:
            return resp
        if _etag_match(etag, req.headers.get("if-none-match", "")):
            return Response(b"", 304, {"etag": etag})
        return resp
