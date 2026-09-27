"""MCP (Model Context Protocol) server derived from compiled route plans. Zero deps.

Every HTTP route becomes an LLM-callable tool: path/query/body params merge into
one JSON inputSchema, auth boundaries stay enforced (401/403 surface as tool
errors, pass tokens via the `headers` argument).

Transport: newline-delimited JSON-RPC 2.0 over stdio (`ikarem mcp myapp:app`).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any
from urllib.parse import urlencode


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", text).strip("_").lower() or "tool"


_ROUTE_TEMPLATE_RE = re.compile(r"\{(\w+)(?::\w+)?\}")


def _tool_name(handler_name: str, method: str, path: str, used: dict[str, tuple]) -> str:
    base = _slug(handler_name)
    key = (method.upper(), path)
    if base not in used or used[base] == key:
        used[base] = key
        return base
    path_nice = _ROUTE_TEMPLATE_RE.sub(r"by_\1", path)
    alt = base + "_" + method.lower() + "_" + _slug(path_nice)
    i, name = 2, alt
    while name in used and used[name] != key:
        name = f"{alt}_{i}"
        i += 1
    used[name] = key
    return name


def _conv_type(converter: str | None) -> str:
    return {"int": "integer", "float": "number", "uuid": "string", "path": "string"}.get(
        converter or "str", "string"
    )


def build_tool(route: Any, desc: Any, name: str) -> dict:
    """Public MCP tool definition for one route."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname in desc.path_params:
        properties[pname] = {
            "type": _conv_type(desc.path_converters.get(pname)),
            "description": "path parameter",
        }
        required.append(pname)
    for q in desc.query:
        if q.name not in properties:
            properties[q.name] = dict(q.schema or {"type": "string"})
            if q.required:
                required.append(q.name)
    body_props = (desc.body_schema or {}).get("properties", {})
    body_required = set((desc.body_schema or {}).get("required", []))
    for bname, bschema in body_props.items():
        if bname not in properties:
            properties[bname] = bschema
            if bname in body_required:
                required.append(bname)
    summary = desc.doc.split("\n")[0] if desc.doc else desc.handler_name
    method = desc.methods[0] if len(desc.methods) == 1 else "/".join(desc.methods)
    description = f"{summary} [HTTP {method} {desc.path}]" if summary else f"HTTP {method} {desc.path}"
    if desc.is_auth:
        roles = f" (roles: {', '.join(desc.auth_roles)})" if desc.auth_roles else ""
        description += f" Requires Authorization header with Bearer JWT{roles}."
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": sorted(set(required))},
    }


def _coerce_arg(value: Any, ann: Any) -> Any:
    if isinstance(value, str):
        if ann is int:
            return int(value)
        if ann is float:
            return float(value)
        if ann is bool:
            return value.lower() in ("1", "true", "yes", "on")
    return value


class MCPServer:
    """Exposes an Ikarem app's routes as MCP tools (list + call + stdio)."""

    def __init__(self, app: Any):
        self.app = app
        self._entries: list[dict] = []
        self._by_name: dict[str, dict] = {}
        self._built = False

    def build(self) -> "MCPServer":
        if self._built:
            return self
        from .compiled import describe_route, get_plan

        used: dict[str, tuple] = {}
        if hasattr(self.app, "_ensure_system_routes"):
            self.app._ensure_system_routes()
        if hasattr(self.app, "compile_all"):
            self.app.compile_all()
        for route in self.app.router.routes:
            if getattr(route.handler, "_ikarem_internal", False):
                continue
            plan = get_plan(route.handler)
            if plan.compile_error:
                continue
            for method in sorted(route.methods):
                from types import SimpleNamespace

                single = SimpleNamespace(
                    path=route.path, methods={method}, handler=route.handler, name=route.name
                )
                desc = describe_route(single)
                desc.methods = [method]
                name = _tool_name(plan.handler_name, method, route.path, used)
                self._entries.append(
                    {
                        "name": name,
                        "route": route,
                        "method": method,
                        "desc": desc,
                        "tool": build_tool(single, desc, name),
                    }
                )
                self._by_name[name] = self._entries[-1]
        self._built = True
        return self

    def list_tools(self) -> list[dict]:
        self.build()
        return [e["tool"] for e in self._entries]

    async def call_tool(self, name: str, args: dict | None) -> dict:
        """Call a tool; always returns MCP {content, isError} (never raises)."""
        self.build()
        entry = self._by_name.get(name)
        if entry is None:
            return _text(f"unknown tool '{name}'", is_error=True)
        return await self._execute(entry, dict(args or {}))

    async def _execute(self, entry: dict, args: dict) -> dict:
        from .compiled import resolve_compiled
        from .di import run_cleanups
        from .errors import HTTPException
        from .http import Request, to_response

        route, method, desc = entry["route"], entry["method"], entry["desc"]
        headers = args.pop("headers", None) or {}
        if not isinstance(headers, dict):
            return _text("'headers' must be an object", is_error=True)

        # Split flat args into path / query / body by the route description.
        path_params: dict[str, Any] = {}
        for pname in desc.path_params:
            if pname not in args:
                return _text(f"missing required path parameter '{pname}'", is_error=True)
            raw = args.pop(pname)
            cast = route.converters.get(pname, str) if hasattr(route, "converters") else str
            try:
                try:
                    path_params[pname] = cast(raw)
                except Exception:
                    path_params[pname] = cast(str(raw))
            except Exception as e:
                return _text(f"invalid path parameter '{pname}': {e}", is_error=True)
        query_names = {q.name for q in desc.query}
        query: dict[str, str] = {}
        for qname in list(args.keys()):
            if qname in query_names:
                query[qname] = args.pop(qname) if isinstance(args[qname], str) else str(args.pop(qname))
        body_names = set(((desc.body_schema or {}).get("properties", {}) or {}).keys())
        body = {k: args.pop(k) for k in list(args.keys()) if k in body_names}
        if args:
            return _text(f"unexpected argument(s): {sorted(args)}", is_error=True)

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": re.sub(
                r"\{(\w+)(?::\w+)?\}", lambda m: str(path_params.get(m.group(1), m.group(0))), route.path
            ),
            "raw_path": route.path.encode(),
            "query_string": urlencode(query).encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "server": ("mcp", 80),
            "client": ("mcp", 5000),
        }
        raw_body = json.dumps(body).encode() if body or desc.body_cls is not None else b""
        sent = False

        async def receive() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        request = Request(scope, receive)
        request.path_params = path_params
        request.app = self.app
        try:
            try:
                result = await resolve_compiled(route.handler, request)
            except HTTPException as e:
                return _text(f"HTTP {e.status_code}: {e.detail}", is_error=True)
            except ValueError as e:
                return _text(f"invalid arguments: {e}", is_error=True)
            except Exception as e:  # noqa: BLE001
                detail = str(e) if getattr(self.app, "debug", False) else "Internal Server Error"
                return _text(f"HTTP 500: {detail}", is_error=True)
            response = to_response(result)
            messages: list[dict] = []

            async def send(msg: dict) -> None:
                messages.append(msg)

            await response(scope, receive, send)
            status, text = 200, ""
            for m in messages:
                if m["type"] == "http.response.start":
                    status = m["status"]
                elif m["type"] == "http.response.body":
                    text += (
                        m.get("body", b"").decode()
                        if isinstance(m.get("body", b""), bytes)
                        else str(m.get("body", ""))
                    )
            if status >= 400:
                return _text(f"HTTP {status}: {text}", is_error=True)
            return _text(text)
        finally:
            try:
                bg = getattr(request, "_bg", None)
                if bg is not None:
                    await bg()
            finally:
                await run_cleanups(request)

    # ---- JSON-RPC over stdio (newline-delimited) ----

    async def handle(self, payload: Any) -> Any | None:
        """Handle one JSON-RPC message; returns response dict or None (notification)."""
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid Request")
        mid, method, params = payload.get("id"), payload.get("method"), payload.get("params") or {}
        is_notification = "id" not in payload
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ikarem", "version": getattr(self.app, "_version", "0.1.0")},
                }
            elif method in ("notifications/initialized", "notifications/cancelled"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.list_tools()}
            elif method == "tools/call":
                if not isinstance(params, dict) or "name" not in params:
                    return None if is_notification else _rpc_error(mid, -32602, "missing tool 'name'")
                result = await self.call_tool(params["name"], params.get("arguments") or {})
            else:
                return None if is_notification else _rpc_error(mid, -32601, f"Method not found: {method}")
        except Exception as e:  # noqa: BLE001
            return None if is_notification else _rpc_error(mid, -32603, str(e))
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def run_stdio(self) -> None:
        await self.app.startup() if hasattr(self.app, "startup") else None
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    _write(
                        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
                    )
                    continue
                resp = await self.handle(payload)
                if resp is not None:
                    _write(resp)
        finally:
            try:
                await self.app.shutdown()
            except Exception:
                pass


def _text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _rpc_error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _write(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()
