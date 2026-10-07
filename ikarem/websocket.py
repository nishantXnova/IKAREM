"""ASGI WebSocket support: routes + handler abstraction."""

from __future__ import annotations

from typing import Any, Callable

from .routing import Route


class WebSocketDisconnect(RuntimeError):
    """The peer went away. Catch this — not bare RuntimeError — around
    receive loops, so real bugs still surface."""


class WebSocket:
    def __init__(self, scope: dict, receive: Any, send: Any):
        self.scope = scope
        self._receive = receive
        self._send = send
        self.path: str = scope.get("path", "/")
        self.path_params: dict = {}
        self.query: dict = {}
        self.headers: dict = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

    async def accept(self) -> None:
        await self._send({"type": "websocket.accept"})

    async def receive_text(self) -> str:
        while True:
            msg = await self._receive()
            t = msg.get("type", "")
            if t in ("websocket.disconnect", "websocket.close"):
                raise WebSocketDisconnect("websocket disconnected")
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
    """WebSocket routes with the same compiled converters as HTTP.

    ``@app.websocket("/ws/{room}")`` captures params into
    ``ws.path_params`` — no more keying rooms off the first message.
    Unknown converters fail at registration, like HTTP routes.
    """

    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, path: str, handler: Callable) -> None:
        self.routes.append(Route(path, {"WS"}, handler))

    def match(self, path: str) -> tuple[Callable | None, dict[str, Any]]:
        for r in self.routes:
            params = r.match(path)
            if params is not None:
                return r.handler, params
        return None, {}


class Room:
    """In-process pub/sub group for websocket handlers. Join on connect,
    leave in a finally, broadcast to the rest::

        room = Room()

        @app.websocket("/chat")
        async def chat(ws):
            await ws.accept()
            await room.join(ws)
            try:
                while True:
                    await room.broadcast(await ws.receive_text(), exclude=ws)
            except WebSocketDisconnect:
                pass
            finally:
                room.leave(ws)

    Single process, no persistence — reach for an external broker when you
    outgrow one node (the interface stays the same shape)."""

    def __init__(self) -> None:
        self._members: set[Any] = set()

    def __len__(self) -> int:
        return len(self._members)

    async def join(self, ws: Any) -> None:
        self._members.add(ws)

    def leave(self, ws: Any) -> None:
        self._members.discard(ws)

    async def broadcast(self, message: Any, exclude: Any = None) -> int:
        """Send text (str) or JSON (anything else) to members. Returns count."""
        dead = []
        n = 0
        for ws in list(self._members):
            if ws is exclude:
                continue
            try:
                if isinstance(message, str):
                    await ws.send_text(message)
                else:
                    await ws.send_json(message)
                n += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._members.discard(ws)
        return n
