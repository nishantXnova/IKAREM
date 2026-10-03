"""MCP (Model Context Protocol) server: routes, functions, and prompts. Zero deps.

Three tool sources, one namespace: every HTTP route becomes an
LLM-callable tool (path/query/body params merge into one JSON
inputSchema, auth boundaries enforced as tool errors); ``@app.tool``
exposes plain functions (Schema params validate); ``@app.prompt``
exposes message templates (``prompts/list`` + ``get``). GET-only safe
reads carry ``readOnlyHint``; everything else makes no claims.

Transport: newline-delimited JSON-RPC 2.0 over stdio (`ikarem mcp myapp:app`).
"""

from __future__ import annotations

import asyncio
import inspect
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


def _fn_schema(fn: Any) -> tuple[dict, list, dict]:
    """(properties, required, annotations) for a plain function.

    Schema-annotated params embed their model schema; everything else
    maps its annotation (unannotated means string). Request, Depends,
    and BackgroundTasks params are refused — tools take plain values.
    """
    import inspect as _inspect

    from .compiled import _hints_of, _is_schema_ann, _json_type

    try:
        sig = _inspect.signature(fn)
    except (ValueError, TypeError):
        raise TypeError(
            f"MCP function '{getattr(fn, '__name__', fn)}' needs a plain signature "
            "(sync or async def with named params)"
        )
    try:
        from .di import Depends
    except ImportError:  # pragma: no cover - di is always present
        Depends = ()  # type: ignore
    hints = _hints_of(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    annotations: dict[str, Any] = {}
    has_default = _inspect.Parameter.empty
    for pname, p in sig.parameters.items():
        if p.kind in (_inspect.Parameter.VAR_POSITIONAL, _inspect.Parameter.VAR_KEYWORD):
            raise TypeError(f"MCP function '{getattr(fn, '__name__', fn)}' takes *{pname}: use named params")
        if isinstance(p.default, Depends):
            raise TypeError(
                f"MCP function '{getattr(fn, '__name__', fn)}' takes Depends() for '{pname}': "
                "tools take plain values, not request-scoped dependencies"
            )
        ann = hints.get(pname, p.annotation)
        if _is_background_ann(ann) or _is_request_ann(ann):
            raise TypeError(
                f"MCP function '{getattr(fn, '__name__', fn)}' takes '{pname}' "
                "as a request/background param: tools take plain values"
            )
        annotations[pname] = None if ann is _inspect._empty else ann
        if ann is not _inspect._empty and _is_schema_ann(ann):
            try:
                properties[pname] = ann.json_schema()
            except Exception:
                properties[pname] = {"type": "object"}
        else:
            properties[pname] = _json_type(None if ann is _inspect._empty else ann)
        if p.default is has_default:
            required.append(pname)
    return properties, sorted(required), annotations


def _is_background_ann(ann: Any) -> bool:
    return inspect.isclass(ann) and getattr(ann, "__name__", "") == "BackgroundTasks"


def _is_request_ann(ann: Any) -> bool:
    try:
        from .http import Request

        return inspect.isclass(ann) and issubclass(ann, Request)
    except Exception:
        return False


def register_custom_tool(app: Any, name: str, fn: Any) -> None:
    """Validate now (fail fast) and stash for the next MCPServer.build()."""
    _fn_schema(fn)  # raises TypeError naming the problem
    app._mcp_custom[name] = fn


def register_prompt(app: Any, name: str, fn: Any) -> None:
    """Validate now (fail fast) and stash for the next MCPServer.build()."""
    _fn_schema(fn)  # raises TypeError naming the problem
    app._mcp_prompts[name] = fn


def _normalize_messages(result: Any, name: str) -> list[dict]:
    """str -> one user message; list of str/dict -> messages. Anything
    else is a usage error naming the contract."""

    def _one(item: Any) -> dict:
        if isinstance(item, str):
            return {"role": "user", "content": [{"type": "text", "text": item}]}
        if isinstance(item, dict) and isinstance(item.get("role"), str):
            content = item.get("content", "")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            return {"role": item["role"], "content": content}
        raise TypeError(
            f"MCP prompt '{name}' must return str or list[str | {{role, content}}], got {type(item).__name__}"
        )

    if isinstance(result, str):
        return [_one(result)]
    if isinstance(result, list):
        return [_one(item) for item in result]
    raise TypeError(
        f"MCP prompt '{name}' must return str or list[str | {{role, content}}], got {type(result).__name__}"
    )


def build_tool(route: Any, desc: Any, name: str, background: bool = False) -> dict:
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
        scopes = (
            f" (scopes: {', '.join(getattr(desc, 'auth_scopes', ()) or ())})"
            if getattr(desc, "auth_scopes", ())
            else ""
        )
        if (getattr(desc, "auth_scheme", "") or "bearer").lower() == "apikey":
            header = getattr(desc, "auth_header", "") or "x-api-key"
            description += f" Requires {header} header with API key{roles}{scopes}."
        else:
            description += f" Requires Authorization header with Bearer JWT{roles}{scopes}."
    tool: dict[str, Any] = {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": sorted(set(required))},
    }
    methods = [m.upper() for m in (desc.methods or [])]
    if methods == ["GET"] and not background:
        # Only claimed when true: safe reads get readOnlyHint, everything
        # else makes no claims (a POST that only reads is still not marked).
        tool["annotations"] = {"readOnlyHint": True}
    return tool


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
        self._prompts: dict[str, Any] = {}
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
                        "tool": build_tool(single, desc, name, background=plan.has_background),
                    }
                )
                self._by_name[name] = self._entries[-1]
        for name, fn in getattr(self.app, "_mcp_custom", {}).items():
            properties, required, annotations = _fn_schema(fn)
            summary = (inspect.getdoc(fn) or "").strip().split("\n")[0] or name
            # Custom names win: drop any route-derived tool they shadow so
            # list and call agree on what `name` means.
            self._entries = [e for e in self._entries if e["name"] != name]
            self._by_name[name] = {
                "name": name,
                "custom": fn,
                "annotations_map": annotations,
                "tool": {
                    "name": name,
                    "description": summary,
                    "inputSchema": {"type": "object", "properties": properties, "required": required},
                },
            }
        for name, fn in getattr(self.app, "_mcp_prompts", {}).items():
            properties, required, annotations = _fn_schema(fn)
            summary = (inspect.getdoc(fn) or "").strip().split("\n")[0] or name
            self._prompts[name] = {
                "fn": fn,
                "annotations_map": annotations,
                "definition": {
                    "name": name,
                    "description": summary,
                    "arguments": [{"name": pname, "required": pname in required} for pname in properties],
                },
            }
        self._built = True
        return self

    def list_tools(self) -> list[dict]:
        self.build()
        return [e["tool"] for e in self._entries] + [
            e["tool"] for e in self._by_name.values() if "custom" in e
        ]

    def list_prompts(self) -> list[dict]:
        self.build()
        return [p["definition"] for p in self._prompts.values()]

    async def get_prompt(self, name: str, args: dict | None) -> dict:
        """Run a prompt; returns MCP {description, messages} (never raises)."""
        self.build()
        entry = self._prompts.get(name)
        if entry is None:
            return {"isError": True, "content": [{"type": "text", "text": f"unknown prompt '{name}'"}]}
        given = dict(args or {})
        required = {a["name"] for a in entry["definition"]["arguments"] if a["required"]}
        known = {a["name"] for a in entry["definition"]["arguments"]}
        missing = [k for k in sorted(required) if k not in given]
        if missing:
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"missing argument(s): {missing}"}],
            }
        unknown = [k for k in sorted(given) if k not in known]
        if unknown:
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"unexpected argument(s): {unknown}"}],
            }
        try:
            kwargs = self._coerce_call(entry, given)
            result = entry["fn"](**kwargs)
            if inspect.isawaitable(result):
                result = await result
            messages = _normalize_messages(result, name)
        except Exception as e:  # noqa: BLE001
            return {"isError": True, "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]}
        return {"description": entry["definition"]["description"], "messages": messages}

    def _coerce_call(self, entry: dict, given: dict) -> dict:
        """Coerce flat args by annotation; Schema models validate."""
        import inspect as _inspect

        annotations_map = entry.get("annotations_map") or {}
        kwargs: dict[str, Any] = {}
        for pname, value in given.items():
            ann = annotations_map.get(pname)
            if ann is not None and ann is not _inspect._empty:
                from .compiled import _is_schema_ann

                if _is_schema_ann(ann):
                    kwargs[pname] = ann.validate(value)
                    continue
                value = _coerce_arg(value, ann)
            kwargs[pname] = value
        return kwargs

    def list_resources(self) -> list[dict]:
        self.build()
        return [
            {"uri": "ikarem://openapi.json", "name": "OpenAPI 3.1 spec", "mimeType": "application/json"},
            {
                "uri": "ikarem://manifest",
                "name": "Compact route manifest for LLM context",
                "mimeType": "application/json",
            },
        ]

    def read_resource(self, uri: str) -> dict:
        """Read a resource; always returns MCP {contents} or {isError} (never raises)."""
        import json as _json

        try:
            self.build()
            if uri == "ikarem://openapi.json":
                from .openapi import build_openapi

                text = _json.dumps(build_openapi(self.app, version=getattr(self.app, "_version", "0.1.0")))
            elif uri == "ikarem://manifest":
                from .compiled import describe_app

                text = _json.dumps(describe_app(self.app))
            else:
                return {"isError": True, "content": [{"type": "text", "text": f"unknown resource '{uri}'"}]}
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}
        except Exception as e:  # noqa: BLE001
            return {"isError": True, "content": [{"type": "text", "text": str(e)}]}

    async def call_tool(self, name: str, args: dict | None) -> dict:
        """Call a tool; always returns MCP {content, isError} (never raises)."""
        self.build()
        entry = self._by_name.get(name)
        if entry is None:
            return _text(f"unknown tool '{name}'", is_error=True)
        if "custom" in entry:
            return await self._execute_custom(entry, dict(args or {}))
        return await self._execute(entry, dict(args or {}))

    async def _execute_custom(self, entry: dict, args: dict) -> dict:
        fn = entry["custom"]
        tool = entry["tool"]
        required = set(tool["inputSchema"].get("required", []))
        known = set(tool["inputSchema"].get("properties", {}))
        missing = [k for k in sorted(required) if k not in args]
        if missing:
            return _text(f"missing argument(s): {missing}", is_error=True)
        unknown = [k for k in sorted(args) if k not in known]
        if unknown:
            return _text(f"unexpected argument(s): {unknown}", is_error=True)
        try:
            kwargs = self._coerce_call(entry, args)
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            text = result if isinstance(result, str) else json.dumps(result)
        except Exception as e:  # noqa: BLE001
            return _text(f"{type(e).__name__}: {e}", is_error=True)
        return _text(text)

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
                self.build()
                capabilities: dict[str, Any] = {"tools": {}, "resources": {}}
                if getattr(self, "_prompts", None):
                    capabilities["prompts"] = {}
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": capabilities,
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
            elif method == "resources/list":
                result = {"resources": self.list_resources()}
            elif method == "resources/read":
                if not isinstance(params, dict) or "uri" not in params:
                    return None if is_notification else _rpc_error(mid, -32602, "missing resource 'uri'")
                result = self.read_resource(params["uri"])
            elif method == "prompts/list":
                result = {"prompts": self.list_prompts()}
            elif method == "prompts/get":
                if not isinstance(params, dict) or "name" not in params:
                    return None if is_notification else _rpc_error(mid, -32602, "missing prompt 'name'")
                outcome = await self.get_prompt(params["name"], params.get("arguments") or {})
                if outcome.get("isError"):
                    return None if is_notification else _rpc_error(mid, -32602, outcome["content"][0]["text"])
                result = outcome
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
