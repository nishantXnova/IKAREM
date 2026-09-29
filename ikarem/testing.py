"""In-process TestClient — exercises the ASGI app with no server."""

from __future__ import annotations

import asyncio
import atexit
import json
import threading
from typing import Any

_tls = threading.local()
_all_loops: list = []
_loops_lock = threading.Lock()


def _client_loop() -> Any:
    """One event loop per thread, shared by all TestClients on it.

    Loop-bound resources (asyncpg pools, sqlite WAL state) survive across
    requests instead of churning per request — closer to a real server,
    and pooled drivers stop leaking a pool per call. Use one TestClient
    per thread (the normal pattern).
    """
    loop = getattr(_tls, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _tls.loop = loop
        with _loops_lock:
            _all_loops.append(loop)
    return loop


@atexit.register
def _close_client_loops() -> None:
    with _loops_lock:
        loops, _all_loops[:] = list(_all_loops), []
    for loop in loops:
        try:
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass


class TestResponse:
    def __init__(self, status: int, headers: dict, body: bytes, headers_list: list | None = None):
        self.status_code = status
        self.headers = headers
        self.headers_list = headers_list or []
        self.body = body
        self.cookies: dict[str, str] = {}
        for k, v in self.headers_list:
            if k.lower() == "set-cookie":
                chunk = v.split(";", 1)[0]
                if "=" in chunk:
                    ck, cv = chunk.strip().split("=", 1)
                    self.cookies[ck] = cv

    def json(self) -> Any:
        return json.loads(self.body.decode() or "null")

    @property
    def text(self) -> str:
        return self.body.decode()


class TestClient:
    __test__ = False

    def __init__(self, app: Any):
        self.app = app
        self.cookies: dict[str, str] = {}  # persisted jar (sessions just work)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | dict | str | None = None,
        headers: dict[str, str] | None = None,
        query: str = "",
        cookies: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> TestResponse:
        # Copy: never mutate the caller's dicts (a shared headers dict must
        # not freeze a stale Cookie across login/logout in the same test).
        return _client_loop().run_until_complete(
            self._do(
                method,
                path,
                body,
                dict(headers or {}),
                query,
                dict(cookies or {}) if cookies else None,
                content_type,
            )
        )

    async def _do(
        self,
        method: str,
        path: str,
        body: Any,
        headers: dict,
        query: str,
        cookies: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> TestResponse:
        if isinstance(body, dict):
            raw = json.dumps(body).encode()
            headers.setdefault("content-type", "application/json")
        elif isinstance(body, str):
            raw = body.encode()
        elif body is None:
            raw = b""
        else:
            raw = body
        if content_type is not None:
            headers["content-type"] = content_type
        jar = dict(self.cookies)
        if cookies:
            jar.update(cookies)
        if jar and "cookie" not in {k.lower() for k in headers}:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "server": ("test", 80),
            "client": ("test", 5000),
        }
        sent_request = False

        async def receive() -> dict:
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        messages: list[dict] = []

        async def send(msg: dict) -> None:
            messages.append(msg)

        # lifespan startup once per request is fine for tests
        await self.app.startup()
        try:
            await self.app(scope, receive, send)
        finally:
            pass
        status, resp_headers, resp_body, raw_headers = 500, {}, b"", []
        for m in messages:
            if m["type"] == "http.response.start":
                status = m["status"]
                raw_headers = [(k.decode(), v.decode()) for k, v in m.get("headers", [])]
                for k, v in raw_headers:
                    resp_headers[k] = v
            elif m["type"] == "http.response.body":
                resp_body += m.get("body", b"")
        resp = TestResponse(status, resp_headers, resp_body, raw_headers)
        for ck, cv in resp.cookies.items():
            if cv == "" and any(
                "max-age=0" in v.lower() for k, v in raw_headers if k.lower() == "set-cookie"
            ):
                self.cookies.pop(ck, None)
            else:
                self.cookies[ck] = cv
        return resp

    def get(self, path: str, **kw: Any) -> TestResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> TestResponse:
        return self.request("POST", path, body=body, **kw)

    def put(self, path: str, body: Any = None, **kw: Any) -> TestResponse:
        return self.request("PUT", path, body=body, **kw)

    def patch(self, path: str, body: Any = None, **kw: Any) -> TestResponse:
        return self.request("PATCH", path, body=body, **kw)

    def delete(self, path: str, **kw: Any) -> TestResponse:
        return self.request("DELETE", path, **kw)

    def clear_cookies(self) -> None:
        self.cookies.clear()

    async def ws_connect(
        self,
        path: str,
        incoming: list[dict] | None = None,
        headers: dict[str, str] | None = None,
        query: str = "",
    ) -> list[dict]:
        """Drive a websocket route in-process: feed `incoming` messages,
        collect everything the handler sends. When the script runs out,
        the connection disconnects (handlers see it via receive_text)."""
        pending = list(incoming or [])

        async def receive() -> dict:
            if pending:
                return pending.pop(0)
            return {"type": "websocket.disconnect"}

        sent: list[dict] = []

        async def send(msg: dict) -> None:
            sent.append(msg)

        scope = {
            "type": "websocket",
            "path": path,
            "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "server": ("test", 80),
            "client": ("test", 5000),
        }
        await self.app.startup()
        await self.app(scope, receive, send)
        return sent
