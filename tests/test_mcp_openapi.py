"""OpenAPI requestBody/query/auth + MCP tools, all derived from compiled plans."""

import asyncio
import json

from ikarem import Depends, Ikarem, Schema, create_token, require_roles
from ikarem.mcp import MCPServer
from ikarem.openapi import build_openapi
from ikarem.testing import TestClient

SECRET = "mcp-test-secret"


def pager(limit: int = 5):
    return limit


class Item(Schema):
    name: str
    qty: int = 1


def build_app():
    app = Ikarem(enable_docs=False, auth_secret=SECRET)

    @app.get("/users/{uid:int}")
    async def get_user(uid: int, limit=Depends(pager)):
        return {"uid": uid, "limit": limit}

    @app.post("/items")
    async def create_item(item: Item):
        """Create an item."""
        return {"name": item.name, "qty": item.qty}

    @app.get("/admin")
    async def admin(req, claims=Depends(require_roles("admin"))):
        return {"sub": claims["sub"]}

    return app


def _tools(app):
    return {t["name"]: t for t in app.mcp_tools()}


# ---- OpenAPI ----


def test_openapi_path_and_nested_query_params():
    spec = build_openapi(build_app())
    op = spec["paths"]["/users/{uid}"]["get"]
    by_name = {p["name"]: p for p in op["parameters"]}
    assert by_name["uid"] == {"name": "uid", "in": "path", "required": True, "schema": {"type": "integer"}}
    # `limit` lives inside the pager dependency but is still documented
    assert by_name["limit"]["in"] == "query"
    assert by_name["limit"]["schema"] == {"type": "integer"}
    assert by_name["limit"]["required"] is False


def test_openapi_request_body_from_schema():
    spec = build_openapi(build_app())
    op = spec["paths"]["/items"]["post"]
    assert op["summary"] == "Create an item."
    body = op["requestBody"]["content"]["application/json"]["schema"]
    assert body["properties"]["name"] == {"type": "string"}
    assert body["required"] == ["name"]
    assert op["responses"]["400"]["description"] == "Validation error"


def test_openapi_auth_responses_and_scheme():
    spec = build_openapi(build_app())
    op = spec["paths"]["/admin"]["get"]
    assert op["responses"]["401"]["description"] == "Unauthorized"
    assert op["responses"]["403"]["description"] == "Forbidden"
    assert op["security"] == [{"bearerAuth": []}]
    assert spec["components"]["securitySchemes"]["bearerAuth"] == {"type": "http", "scheme": "bearer"}


# ---- MCP tools ----


def test_mcp_tool_names_and_schemas():
    tools = _tools(build_app())
    assert set(tools) == {"get_user", "create_item", "admin"}
    schema = tools["get_user"]["inputSchema"]
    assert schema["properties"]["uid"]["type"] == "integer"
    assert "uid" in schema["required"]
    assert "limit" not in schema["required"]
    assert "Requires Authorization" in tools["admin"]["description"]
    assert (
        "requires" not in tools["get_user"]["description"].lower()
        or "Authorization" not in tools["get_user"]["description"]
    )


def test_mcp_call_path_and_query():
    app = build_app()
    out = asyncio.run(app.mcp_call("get_user", {"uid": 3}))
    assert out["isError"] is False
    assert json.loads(out["content"][0]["text"]) == {"uid": 3, "limit": 5}
    out = asyncio.run(app.mcp_call("get_user", {"uid": "7", "limit": "2"}))
    assert json.loads(out["content"][0]["text"]) == {"uid": 7, "limit": 2}


def test_mcp_call_body_and_validation_error():
    app = build_app()
    out = asyncio.run(app.mcp_call("create_item", {"name": "apple", "qty": 2}))
    assert json.loads(out["content"][0]["text"]) == {"name": "apple", "qty": 2}
    bad = asyncio.run(app.mcp_call("create_item", {"qty": 1}))
    assert bad["isError"] is True


def test_mcp_call_auth_boundary():
    app = build_app()
    anon = asyncio.run(app.mcp_call("admin", {}))
    assert anon["isError"] is True and "401" in anon["content"][0]["text"]
    user_tok = create_token("u2", SECRET, roles=["user"])
    denied = asyncio.run(app.mcp_call("admin", {"headers": {"authorization": f"Bearer {user_tok}"}}))
    assert denied["isError"] is True and "403" in denied["content"][0]["text"]
    admin_tok = create_token("u1", SECRET, roles=["admin"])
    ok = asyncio.run(app.mcp_call("admin", {"headers": {"authorization": f"Bearer {admin_tok}"}}))
    assert ok["isError"] is False
    assert json.loads(ok["content"][0]["text"]) == {"sub": "u1"}


def test_mcp_call_rejects_unknown_tool_and_args():
    app = build_app()
    assert asyncio.run(app.mcp_call("nope", {}))["isError"] is True
    out = asyncio.run(app.mcp_call("get_user", {"uid": 1, "bogus": 2}))
    assert out["isError"] is True and "bogus" in out["content"][0]["text"]
    out = asyncio.run(app.mcp_call("get_user", {}))
    assert out["isError"] is True and "uid" in out["content"][0]["text"]


def test_mcp_jsonrpc_protocol():
    srv = MCPServer(build_app())

    async def _go():
        init = await srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert init["result"]["capabilities"] == {"tools": {}, "resources": {}}
        assert await srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        listed = await srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {t["name"] for t in listed["result"]["tools"]} == {"get_user", "create_item", "admin"}
        called = await srv.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_user", "arguments": {"uid": 9}},
            }
        )
        assert called["result"]["isError"] is False
        assert json.loads(called["result"]["content"][0]["text"])["uid"] == 9
        missing = await srv.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        assert missing["error"]["code"] == -32601
        assert (await srv.handle({"a": 1}))["error"]["code"] == -32600

    asyncio.run(_go())


def test_http_behavior_unchanged():
    c = TestClient(build_app())
    assert c.get("/users/3", query="limit=2").json() == {"uid": 3, "limit": 2}
    assert c.post("/items", body={"name": "x"}).json() == {"name": "x", "qty": 1}
