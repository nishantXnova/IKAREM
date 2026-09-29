"""Request / Response abstractions — decoupled from ASGI specifics.

Handlers never see `scope/receive/send`. They get a Request and return
a Response (or dict/list/str/bytes which are auto-converted).
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl


class Request:
    def __init__(self, scope: dict, receive: Callable[[], Awaitable[dict]]):
        assert scope["type"] == "http"
        self.scope = scope
        self._receive = receive
        self.method: str = scope.get("method", "GET").upper()
        self.path: str = scope.get("path", "/")
        self.query_string: bytes = scope.get("query_string", b"")
        self.headers: dict[str, str] = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        self.path_params: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.app: Any = None
        self._body: bytes | None = None

    @property
    def query(self) -> dict[str, str]:
        return dict(parse_qsl(self.query_string.decode()))

    @property
    def cookies(self) -> dict[str, str]:
        raw = self.headers.get("cookie", "")
        out: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    async def body(self, max_bytes: int | None = None) -> bytes:
        if self._body is None:
            from .errors import PayloadTooLarge

            chunks: list[bytes] = []
            total = 0
            while True:
                msg = await self._receive()
                if msg["type"] == "http.request":
                    chunk = msg.get("body", b"")
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise PayloadTooLarge(f"body exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                    if not msg.get("more_body"):
                        break
                elif msg["type"] == "http.disconnect":
                    break
            self._body = b"".join(chunks)
        elif max_bytes is not None and len(self._body) > max_bytes:
            from .errors import PayloadTooLarge

            raise PayloadTooLarge(f"body exceeds {max_bytes} bytes")
        return self._body

    async def json(self, max_bytes: int = 10 * 1024 * 1024) -> Any:
        return json.loads((await self.body(max_bytes)).decode() or "null")

    async def text(self, max_bytes: int | None = None) -> str:
        return (await self.body(max_bytes)).decode()

    async def form(
        self,
        max_form_size: int = 10 * 1024 * 1024,
        max_file_size: int = 10 * 1024 * 1024,
    ) -> "FormData":
        """Parse urlencoded or multipart forms (files included). Zero deps.

        Caps total size and per-file size; oversized payloads raise
        PayloadTooLarge (413). Repeated fields keep all values (getlist).
        """
        from .errors import PayloadTooLarge

        raw = await self.body(max_form_size)
        ctype = self.headers.get("content-type", "")
        mime = ctype.split(";")[0].strip().lower()
        if mime == "multipart/form-data":
            boundary = _multipart_boundary(ctype)
            if not boundary:
                raise PayloadTooLarge("multipart form missing boundary")
            return _parse_multipart(raw, ctype, max_file_size)
        # default: application/x-www-form-urlencoded (also empty bodies)
        try:
            text = raw.decode()
        except UnicodeDecodeError:
            raise PayloadTooLarge("form is not decodable text")
        multi: dict[str, list] = {}
        for k, v in parse_qsl(text, keep_blank_values=True):
            multi.setdefault(k, []).append(v)
        return FormData(multi)


class UploadFile:
    """An uploaded file held in memory (cap via max_file_size on req.form())."""

    def __init__(self, filename: str, content_type: str, data: bytes):
        self.filename = filename
        self.content_type = content_type or "application/octet-stream"
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    async def read(self) -> bytes:
        return self._data

    def write(self, path: str) -> str:
        with open(path, "wb") as f:
            f.write(self._data)
        return path

    def __repr__(self) -> str:  # pragma: no cover
        return f"UploadFile({self.filename!r}, {self.size} bytes)"


class FormData:
    """Multi-value form mapping; file fields hold UploadFile values."""

    def __init__(self, multi: dict[str, list] | None = None):
        self._multi: dict[str, list] = dict(multi or {})

    def getlist(self, key: str) -> list:
        return list(self._multi.get(key, []))

    def get(self, key: str, default: Any = None) -> Any:
        vals = self._multi.get(key)
        return vals[0] if vals else default

    def __getitem__(self, key: str) -> Any:
        vals = self._multi.get(key)
        if not vals:
            raise KeyError(key)
        return vals[0]

    def __contains__(self, key: object) -> bool:
        return key in self._multi

    def keys(self):
        return self._multi.keys()

    def items(self):
        return ((k, v[0]) for k, v in self._multi.items() if v)

    def multi_items(self):
        for k, vals in self._multi.items():
            for v in vals:
                yield k, v

    def __len__(self) -> int:
        return len(self._multi)

    def __repr__(self) -> str:  # pragma: no cover
        return f"FormData({ {k: ('<file>' if isinstance(v[0], UploadFile) else v[0]) for k, v in self._multi.items()}!r})"


def _multipart_boundary(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k.strip().lower() == "boundary":
                return v.strip().strip('"')
    return ""


def _parse_multipart(raw: bytes, content_type: str, max_file_size: int) -> FormData:
    from email.parser import BytesParser
    from email.policy import HTTP

    from .errors import PayloadTooLarge

    try:
        head = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    except (UnicodeEncodeError, ValueError):
        raise PayloadTooLarge("multipart content-type is not decodable")
    try:
        msg = BytesParser(policy=HTTP).parsebytes(head + raw)
    except Exception:
        raise PayloadTooLarge("malformed multipart body")
    if not msg.is_multipart():
        raise PayloadTooLarge("malformed multipart body")
    multi: dict[str, list] = {}
    for part in msg.iter_parts():
        disp = part.get_content_disposition() or ""
        if disp != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            if len(payload) > max_file_size:
                raise PayloadTooLarge(f"file '{filename}' exceeds {max_file_size} bytes")
            multi.setdefault(name, []).append(UploadFile(filename, part.get_content_type(), payload))
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                multi.setdefault(name, []).append(payload.decode(charset))
            except UnicodeDecodeError:
                raise PayloadTooLarge(f"field '{name}' is not decodable text")
    return FormData(multi)


class Response:
    def __init__(
        self,
        content: bytes | str | None = b"",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        media_type: str = "text/plain",
    ):
        if isinstance(content, str):
            content = content.encode()
        self.body: bytes = content or b""
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        headers = [(b"content-type", self.media_type.encode())]
        for k, v in self.headers.items():
            if isinstance(v, (list, tuple)):
                for item in v:
                    headers.append((k.lower().encode(), str(item).encode()))
            else:
                headers.append((k.lower().encode(), str(v).encode()))
        headers.append((b"content-length", str(len(self.body)).encode()))
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": self.body, "more_body": False})

    def set_cookie(
        self,
        key: str,
        value: str,
        max_age: int | None = None,
        path: str = "/",
        domain: str | None = None,
        httponly: bool = True,
        samesite: str = "Lax",
        secure: bool = False,
    ) -> None:
        set_cookie(self.headers, key, value, max_age, path, domain, httponly, samesite, secure)

    def delete_cookie(self, key: str, path: str = "/") -> None:
        delete_cookie(self.headers, key, path)


def set_cookie(
    headers: dict,
    key: str,
    value: str,
    max_age: int | None = None,
    path: str = "/",
    domain: str | None = None,
    httponly: bool = True,
    samesite: str = "Lax",
    secure: bool = False,
) -> None:
    """Shared cookie writer for Response + FileResponse (multi Set-Cookie safe)."""
    parts = [f"{key}={value}", f"Path={path}"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    if domain:
        parts.append(f"Domain={domain}")
    if httponly:
        parts.append("HttpOnly")
    if samesite:
        parts.append(f"SameSite={samesite}")
    if secure:
        parts.append("Secure")
    cookie = "; ".join(parts)
    existing = headers.get("set-cookie")
    if existing is None:
        headers["set-cookie"] = [cookie]
    elif isinstance(existing, list):
        existing.append(cookie)
    else:
        headers["set-cookie"] = [existing, cookie]


def delete_cookie(headers: dict, key: str, path: str = "/") -> None:
    set_cookie(headers, key, "", max_age=0, path=path)


def escape_html(s: Any) -> str:
    """XSS-safe interpolation for hand-built HTML (Jinja2 autoescapes already)."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def dict_to_xml(data: Any, root: str = "response") -> str:
    """Tiny XML serializer for dict/list/scalar trees (lists repeat <item>)."""

    def _node(tag: str, value: Any) -> str:
        if isinstance(value, dict):
            inner = "".join(_node(str(k), v) for k, v in value.items())
            return f"<{tag}>{inner}</{tag}>"
        if isinstance(value, (list, tuple)):
            inner = "".join(_node("item", v) for v in value)
            return f"<{tag}>{inner}</{tag}>"
        if value is None:
            return f"<{tag}/>"
        if isinstance(value, bool):
            return f"<{tag}>{str(value).lower()}</{tag}>"
        return f"<{tag}>{escape_html(value)}</{tag}>"

    return '<?xml version="1.0" encoding="UTF-8"?>' + _node(root, data)


class XMLResponse(Response):
    def __init__(
        self, data: Any, status_code: int = 200, headers: dict[str, str] | None = None, root: str = "response"
    ):
        super().__init__(
            dict_to_xml(data, root).encode(),
            status_code=status_code,
            headers=headers,
            media_type="application/xml",
        )


class JSONResponse(Response):
    def __init__(self, data: Any, status_code: int = 200, headers: dict[str, str] | None = None):
        super().__init__(
            json.dumps(data).encode(),
            status_code=status_code,
            headers=headers,
            media_type="application/json",
        )


class TextResponse(Response):
    def __init__(self, text: str, status_code: int = 200, headers: dict[str, str] | None = None):
        super().__init__(text, status_code=status_code, headers=headers, media_type="text/plain")


class HTMLResponse(Response):
    def __init__(self, html: str, status_code: int = 200, headers: dict[str, str] | None = None):
        super().__init__(html, status_code=status_code, headers=headers, media_type="text/html")


class RedirectResponse(Response):
    def __init__(self, url: str, status_code: int = 307):
        super().__init__(b"", status_code=status_code, headers={"location": url})


class StreamingResponse(Response):
    """Minimal streaming helper — still a Response, sends chunks."""

    def __init__(self, iterator: Any, status_code: int = 200, media_type: str = "text/plain"):
        self.iterator = iterator
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.media_type = media_type
        self.body = b""

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        headers = [(b"content-type", self.media_type.encode())]
        for k, v in self.headers.items():
            if isinstance(v, (list, tuple)):
                for item in v:
                    headers.append((k.lower().encode(), str(item).encode()))
            else:
                headers.append((k.lower().encode(), str(v).encode()))
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": headers,
            }
        )
        # support sync or async iterables
        if hasattr(self.iterator, "__aiter__"):
            async for chunk in self.iterator:  # type: ignore
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        else:
            for chunk in self.iterator:
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def to_response(value: Any) -> Response:
    if value is None:
        return Response(b"", 204)
    if isinstance(value, Response):
        return value
    # FileResponse / StreamingResponse / any ASGI-callable response
    if hasattr(value, "__call__") and hasattr(value, "status_code"):
        return value
    if isinstance(value, (dict, list)):
        return JSONResponse(value)
    if isinstance(value, bytes):
        return Response(value, media_type="application/octet-stream")
    if isinstance(value, str):
        return TextResponse(value)
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], int):
        # (body, status)
        return to_response(value[0]).__class__ and _with_status(to_response(value[0]), value[1])
    return TextResponse(str(value))


def _with_status(resp: Response, status: int) -> Response:
    resp.status_code = status
    return resp
