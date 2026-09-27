"""ASGI WebSocket support: routes + handler abstraction."""

from __future__ import annotations

from typing import Any, Callable


class WebSocket:
    def __init__(self, scope: dict, receive: Any, send: Any):
        self.scope = scope
        self._receive = receive
        self._send = send
        self.path: str = scope.get("path", "/")
        self.query: dict = {}
        self.headers: dict = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

    async def accept(self) -> None:
        await self._send({"type": "websocket.accept"})

    async def receive_text(self) -> str:
        while True:
            msg = await self._receive()
            t = msg.get("type", "")
            if t in ("websocket.disconnect", "websocket.close"):
                raise RuntimeError("websocket disconnected")
            if "text" in msg:
                return msg["text"]
            # skip handshake/control frames (e.g. websocket.connect)

    async def receive_json(self) -> Any:
        import json

        return json.loads(await self.receive_text() or "null")

    async def send_text(self, text: str) -> None:
        await self._send({"type": "websocket.send", "text": text})

    async def send_json(self, data: Any) -> None:
        import json

        await self.send_text(json.dumps(data))

    async def close(self, code: int = 1000) -> None:
        await self._send({"type": "websocket.close", "code": code})


class WSRouter:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Callable]] = []

    def add(self, path: str, handler: Callable) -> None:
        self.routes.append((path, handler))

    def match(self, path: str) -> Callable | None:
        for p, h in self.routes:
            if p == path:
                return h
        return None
