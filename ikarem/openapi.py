"""OpenAPI 3.1 auto-gen from compiled route descriptions + docstrings + Schema models."""

from __future__ import annotations

import re as _re
from typing import Any

_PARAM_RE = _re.compile(r"\{(\w+)(?::(\w+))?\}")
_ROUTE_PARAM_RE = _PARAM_RE


def build_openapi(app: Any, title: str = "IKAREM", version: str = "0.1.0") -> dict:
    from .compiled import describe_route

    paths: dict[str, dict] = {}
    schemes: set[str] = set()
    api_key_headers: set[str] = set()
    for r in app.router.routes:
        # skip auto-mounted internals if flagged
        if getattr(r.handler, "_ikarem_internal", False):
            continue
        desc = describe_route(r)
        oapi_path = _ROUTE_PARAM_RE.sub(r"{\1}", r.path)
        item = paths.setdefault(oapi_path, {})
        params: list[dict] = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": _conv(desc.path_converters.get(name))},
            }
            for name in desc.path_params
        ]
        for q in desc.query:
            params.append(
                {
                    "name": q.name,
                    "in": "query",
                    "required": q.required,
                    "schema": q.schema or {"type": "string"},
                }
            )
        doc = desc.doc
        for method in sorted(r.methods):
            op: dict[str, Any] = {
                "summary": doc.split("\n")[0] if doc else desc.handler_name,
                "description": doc,
                "parameters": params,
                "responses": {"200": {"description": "OK"}},
            }
            if desc.body_schema:
                op["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": desc.body_schema}},
                }
                op["responses"]["400"] = {"description": "Validation error"}
            elif desc.query:
                op["responses"]["400"] = {"description": "Invalid query parameter"}
            if desc.is_auth:
                scheme = (desc.auth_scheme or "bearer").lower()
                if scheme == "apikey":
                    header = desc.auth_header or "x-api-key"
                    api_key_headers.add(header)
                    schemes.add("apiKey")
                    op["security"] = [{"apiKeyAuth": []}]
                else:
                    schemes.add("bearer")
                    op["security"] = [{"bearerAuth": []}]
                op["responses"]["401"] = {"description": "Unauthorized"}
                op["responses"]["403"] = {"description": "Forbidden"}
            item[method.lower()] = op
    spec: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    if schemes:
        security_schemes: dict[str, Any] = {}
        if "bearer" in schemes:
            security_schemes["bearerAuth"] = {"type": "http", "scheme": "bearer"}
        if "apiKey" in schemes:
            header = sorted(api_key_headers)[0] if api_key_headers else "x-api-key"
            security_schemes["apiKeyAuth"] = {"type": "apiKey", "in": "header", "name": header}
        spec["components"] = {"securitySchemes": security_schemes}
    return spec


def _conv(c: str | None) -> str:
    return {"int": "integer", "float": "number", "uuid": "string", "path": "string"}.get(c or "str", "string")


DOCS_HTML = """<!doctype html><html><head><meta charset="utf-8"/>
<title>{title} — docs</title>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"/>
</head><body><div id="ui"></div>
<script>SwaggerUIBundle({{url:"{spec_url}",dom_id:"#ui"}})</script></body></html>"""


def mount_docs(app: Any, title: str = "IKAREM", spec_url: str = "/openapi.json") -> None:
    from .http import HTMLResponse, JSONResponse

    async def _spec(req: Any) -> Any:
        return JSONResponse(build_openapi(app, title=title, version=getattr(app, "_version", "0.1.0")))

    async def _docs(req: Any) -> Any:
        return HTMLResponse(DOCS_HTML.format(title=title, spec_url=spec_url))

    _spec._ikarem_internal = True  # type: ignore
    _docs._ikarem_internal = True  # type: ignore
    # No silent try/except: _docs_mounted guards double-mount; real
    # failures must surface.
    app.router.add(spec_url, {"GET"}, _spec, name="openapi")
    app.router.add("/docs", {"GET"}, _docs, name="docs")
