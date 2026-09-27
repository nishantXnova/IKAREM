"""Central application object + ASGI callable + lifecycle."""

from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable

from .config import Config
from .errors import ExceptionHandlers, HTTPException
from .http import JSONResponse, Request, Response, to_response
from .middleware import MiddlewareStack
from .plugins import PluginManager
from .routing import Router


class Ikarem:
    def __init__(self, debug: bool = False, enable_docs: bool = True, version: str = "0.1.0", **config: Any):
        self.config = Config({"debug": debug}, **config)
        self.debug = bool(self.config.get("debug", False))
        self._version = version
        self.router = Router()
        self.middleware = MiddlewareStack()
        self.plugins = PluginManager()
        self.exceptions = ExceptionHandlers()
        self._startup: list[Callable] = []
        self._shutdown: list[Callable] = []
        self._started = False
        self._docs_mounted = False
        self._enable_docs = enable_docs
        from .websocket import WSRouter

        self.ws_router = WSRouter()

    # ---- routing sugar ----
    def route(self, path: str, methods: list[str]):
        return self.router.route(path, methods)

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

    def websocket(self, path: str):
        def deco(fn: Callable) -> Callable:
            self.ws_router.add(path, fn)
            return fn

        return deco

    def include_router(self, router: Any, prefix: str = "") -> None:
        for r in router.routes:
            self.router.add(prefix + r.path, r.methods, r.handler, r.name)

    def mount_static(self, url_path: str, directory: str) -> None:
        from .static import static_handler

        self.router.add(url_path.rstrip("/") + "/{path:path}", {"GET"}, static_handler(directory))

    def _ensure_system_routes(self) -> None:
        if self._docs_mounted or not self._enable_docs:
            return
        from .observability import mount_system_routes
        from .openapi import mount_docs

        mount_system_routes(self)
        mount_docs(self)
        self._docs_mounted = True

    def compile_all(self) -> None:
        """Pre-compile every route handler's DI graph once (idempotent)."""
        from .compiled import get_plan

        for r in self.router.routes:
            get_plan(r.handler)

    def check(self) -> dict:
        """Static audit: circular deps, bare Depends(), untyped fallbacks."""
        from .compiled import check_app

        self._ensure_system_routes()
        self.compile_all()
        return check_app(self)

    def mcp_tools(self) -> list[dict]:
        """Every route as an MCP tool definition (name/description/inputSchema)."""
        from .mcp import MCPServer

        return MCPServer(self).list_tools()

    async def mcp_call(self, name: str, args: dict | None = None) -> dict:
        """Call a route-as-tool; returns MCP {content, isError} (never raises)."""
        from .mcp import MCPServer

        return await MCPServer(self).call_tool(name, args or {})

    def mcp_server(self) -> Any:
        """MCP server over stdio (see `ikarem mcp`)."""
        from .mcp import MCPServer

        return MCPServer(self)

    # ---- extension points ----
    def use(self, middleware: Any) -> None:
        self.middleware.add(middleware)

    def register(self, plugin: Any) -> None:
        """Register plugin: calls plugin.register(app) immediately, sorts deps."""
        self.plugins.add(plugin)
        reg = getattr(plugin, "register", None)
        if reg:
            reg(self)

    def on_startup(self, fn: Callable) -> Callable:
        self._startup.append(fn)
        return fn

    def on_shutdown(self, fn: Callable) -> Callable:
        self._shutdown.append(fn)
        return fn

    def exception_handler(self, exc_type: type[Exception]):
        def deco(fn: Callable) -> Callable:
            self.exceptions.add(exc_type, fn)
            return fn

        return deco

    # ---- lifecycle ----
    async def startup(self) -> None:
        if self._started:
            return
        self._ensure_system_routes()
        self.compile_all()  # pre-parse every handler once; per-request has zero reflection
        for fn in self._startup:
            await fn() if inspect.iscoroutinefunction(fn) else fn()
        await self.plugins.startup(self)
        self._started = True

    async def shutdown(self) -> None:
        await self.plugins.shutdown(self)
        for fn in self._shutdown:
            await fn() if inspect.iscoroutinefunction(fn) else fn()
        self._started = False

    # ---- request handling ----
    async def _terminal(self, request: Request) -> Response:
        from .di import resolve_handler

        handler, params = self.router.match(request.method, request.path)
        request.path_params = params
        request.app = self
        result = await resolve_handler(handler, request)
        response = to_response(result)
        # BackgroundTasks param support: stash on response, run after send
        bg = getattr(request, "_bg", None)
        if bg is not None:
            response._background = bg  # type: ignore
        return response

    async def _terminal_safe(self, request: Request) -> Response:
        """Terminal with handler errors rendered INSIDE the middleware pipeline,
        so error responses still get request-ID, security, CORS and rate-limit
        headers from middleware after-hooks."""
        try:
            return await self._terminal(request)
        except HTTPException as e:
            return await self._render_exception(request, e, e.status_code, e.detail)
        except Exception as e:  # noqa: BLE001
            if self.debug:
                tb = "".join(traceback.format_exception(e))
                return JSONResponse({"detail": str(e), "traceback": tb}, status_code=500)
            return await self._render_exception(request, e, 500, "Internal Server Error")

    async def _handle_http(self, scope: dict, receive: Any, send: Any) -> None:
        request = Request(scope, receive)
        request.app = self
        try:
            for p in self.plugins.plugins:
                hook = getattr(p, "on_request", None)
                if hook:
                    await hook(request) if inspect.iscoroutinefunction(hook) else hook(request)
            response = await self.middleware.run(request, self._terminal_safe)
            for p in self.plugins.plugins:
                hook = getattr(p, "on_response", None)
                if hook:
                    await hook(request, response) if inspect.iscoroutinefunction(hook) else hook(
                        request, response
                    )
        except HTTPException as e:
            # Raised by middleware/plugins above the pipeline — same rendering.
            response = await self._render_exception(request, e, e.status_code, e.detail)
        except Exception as e:  # noqa: BLE001
            if self.debug:
                tb = "".join(traceback.format_exception(e))
                response = JSONResponse({"detail": str(e), "traceback": tb}, status_code=500)
            else:
                response = await self._render_exception(request, e, 500, "Internal Server Error")
        try:
            await response(scope, receive, send)
        finally:
            # Background tasks + yield-dependency finalizers ALWAYS run,
            # even if sending fails. Cleanup order: tasks, then dep exits.
            from .di import run_cleanups

            bg = getattr(response, "_background", None)
            if bg is not None:
                await bg()
            await run_cleanups(request)

    async def _render_exception(self, request: Request, exc: Exception, status: int, detail: str) -> Response:
        handler = self.exceptions.find(exc)
        if handler:
            try:
                result = handler(request, exc)
                if inspect.isawaitable(result):
                    result = await result
                return to_response(result)
            except Exception:
                pass
        if isinstance(exc, HTTPException):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if status == 404:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if status == 405:
            return JSONResponse({"detail": detail}, status_code=405)
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    # ---- ASGI ----
    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await self._handle_ws(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        await self._handle_http(scope, receive, send)

    async def _handle_ws(self, scope: dict, receive: Any, send: Any) -> None:
        from .websocket import WebSocket

        handler = self.ws_router.match(scope.get("path", "/"))
        if handler is None:
            await send({"type": "websocket.close", "code": 4404})
            return
        ws = WebSocket(scope, receive, send)
        ws.app = self  # type: ignore
        try:
            result = handler(ws)
            if inspect.isawaitable(result):
                await result
        except Exception:
            try:
                await send({"type": "websocket.close", "code": 1011})
            except Exception:
                pass

    async def _lifespan(self, scope: dict, receive: Any, send: Any) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                try:
                    await self.startup()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as e:
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
            elif msg["type"] == "lifespan.shutdown":
                try:
                    await self.shutdown()
                    await send({"type": "lifespan.shutdown.complete"})
                except Exception as e:
                    await send({"type": "lifespan.shutdown.failed", "message": str(e)})
                return

    def run(self, host: str = "127.0.0.1", port: int = 8000, reload: bool = False, **kw: Any) -> None:
        try:
            import uvicorn
        except ImportError as e:
            raise RuntimeError("pip install ikarem[server] to use app.run() (needs uvicorn)") from e
        uvicorn.run(self, host=host, port=port, reload=reload, **kw)  # type: ignore
