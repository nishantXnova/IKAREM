"""Static files + FileResponse (uploads parse as JSON/multipart-lite)."""

from __future__ import annotations

import mimetypes
import os
from typing import Any


class FileResponse:
    status_code: int = 200

    def __init__(
        self,
        path: str,
        media_type: str | None = None,
        filename: str | None = None,
        status_code: int = 200,
        headers: dict | None = None,
    ):
        self.path = path
        self.media_type = media_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.filename = filename
        self.status_code = status_code
        self.headers: dict = dict(headers or {})

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        with open(self.path, "rb") as f:
            body = f.read()
        headers = [(b"content-type", self.media_type.encode()), (b"content-length", str(len(body)).encode())]
        for k, v in self.headers.items():
            if isinstance(v, (list, tuple)):
                for item in v:
                    headers.append((k.lower().encode(), str(item).encode()))
            else:
                headers.append((k.lower().encode(), str(v).encode()))
        if self.filename:
            headers.append((b"content-disposition", f'attachment; filename="{self.filename}"'.encode()))
        await send({"type": "http.response.start", "status": self.status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    def set_cookie(self, key: str, value: str, **kw: Any) -> None:
        from .http import set_cookie

        set_cookie(self.headers, key, value, **kw)

    def delete_cookie(self, key: str, path: str = "/") -> None:
        from .http import delete_cookie

        delete_cookie(self.headers, key, path)


def static_handler(directory: str) -> Any:
    directory = os.path.abspath(directory)

    async def _h(request: Any) -> Any:
        from .errors import NotFound

        rel = (request.path_params.get("path") or "").lstrip("/")
        full = os.path.abspath(os.path.join(directory, rel))
        if not full.startswith(directory) or not os.path.isfile(full):
            raise NotFound("static file not found")
        return FileResponse(full)

    return _h
