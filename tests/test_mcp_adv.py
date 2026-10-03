"""MCP beyond routes: custom tools, prompts, and honest annotations."""

import asyncio

import pytest

from ikarem import Depends, Ikarem, Schema


def _app():
    app = Ikarem(enable_docs=False)

    @app.tool()
    def add(a: int, b: int = 1):
        """Add two numbers."""
        return a + b

    @app.tool("shout")
    async def _shout(text: str):
        return text.upper()

    class Item(Schema):
        name: str
        qty: int = 1

    @app.tool()
    async def stock(item: Item):
        return {"name": item.name, "qty": item.qty}

    @app.get("/users/{uid:int}")
    async def get_user(req, uid: int):
        return {"uid": uid}

    @app.post("/boom")
    async def boom(req, bg=None):
        raise ValueError("kaput")

    return app


def test_custom_tool_list_call_coerce():
    tools = {t["name"]: t for t in _app().mcp_tools()}
    assert tools["add"]["description"] == "Add two numbers."
    assert tools["add"]["inputSchema"]["required"] == ["a"]
    assert tools["add"]["inputSchema"]["properties"]["a"] == {"type": "integer"}
    assert "annotations" not in tools["add"]  # functions make no safety claims

    async def _go():
        app = _app()
        ok = await app.mcp_call("add", {"a": "3"})
        assert ok["isError"] is False and ok["content"][0]["text"] == "4"
        assert (await app.mcp_call("shout", {"text": "hi"}))["content"][0]["text"] == "HI"
        assert (await app.mcp_call("stock", {"item": {"name": "x"}}))["content"][0]["text"]
        bad = await app.mcp_call("stock", {"item": {"qty": 1}})
        assert bad["isError"] is True
        assert (await app.mcp_call("add", {}))["isError"] is True
        assert (await app.mcp_call("add", {"a": 1, "bogus": 2}))["isError"] is True
        assert (await app.mcp_call("nope", {}))["isError"] is True

    asyncio.run(_go())


def test_custom_tool_error_is_error_not_crash():
    async def _go():
        out = await _app().mcp_call("boom", {})
        assert out["isError"] is True and "kaput" in out["content"][0]["text"]

    asyncio.run(_go())


def test_custom_tool_rejects_request_scoped_params():
    from ikarem import BackgroundTasks

    app = Ikarem(enable_docs=False)
    with pytest.raises(TypeError, match="plain values"):

        @app.tool()
        async def bad(req, db=Depends(lambda: 1)):
            return {"ok": True}

    with pytest.raises(TypeError, match="plain values"):

        @app.tool()
        async def bad2(bg: BackgroundTasks):
            return {"ok": True}


def test_route_tool_annotations_only_when_true():
    tools = {t["name"]: t for t in _app().mcp_tools()}
    assert tools["get_user"].get("annotations") == {"readOnlyHint": True}
    assert "annotations" not in tools["boom"]
    assert "annotations" not in tools["add"]


def _prompt_app():
    app = Ikarem(enable_docs=False)

    @app.prompt()
    def review(code: str, strict: bool = False):
        """Review code for bugs."""
        extra = " Be merciless." if strict else ""
        return f"Review this:{extra}\n{code}"

    @app.prompt("plan")
    async def _plan(goal: str):
        return [
            {"role": "user", "content": f"Goal: {goal}"},
            {"role": "assistant", "content": "Steps: one, two."},
        ]

    return app


def test_prompts_list_get_shapes():
    app = _prompt_app()
    defs = {p["name"]: p for p in app.mcp_server().list_prompts()}
    assert defs["review"]["description"] == "Review code for bugs."
    assert {a["name"] for a in defs["review"]["arguments"]} == {"code", "strict"}
    assert [a["required"] for a in defs["review"]["arguments"] if a["name"] == "code"] == [True]

    async def _go():
        out = await app.mcp_server().get_prompt("review", {"code": "x = 1"})
        assert "isError" not in out
        assert out["messages"][0]["role"] == "user"
        assert "x = 1" in out["messages"][0]["content"][0]["text"]
        plan = await app.mcp_server().get_prompt("plan", {"goal": "ship"})
        assert [m["role"] for m in plan["messages"]] == ["user", "assistant"]
        assert (await app.mcp_server().get_prompt("nope", {}))["isError"] is True
        assert (await app.mcp_server().get_prompt("review", {}))["isError"] is True

    asyncio.run(_go())


def test_prompt_bad_return_is_error():
    app = Ikarem(enable_docs=False)

    @app.prompt()
    def bad():
        return 42

    async def _go():
        out = await app.mcp_server().get_prompt("bad", {})
        assert out["isError"] is True and "str or list" in out["content"][0]["text"]

    asyncio.run(_go())


def test_jsonrpc_prompts_and_capabilities():
    from ikarem.mcp import MCPServer

    async def _go():
        srv = MCPServer(_prompt_app())
        init = await srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert init["result"]["capabilities"]["prompts"] == {}
        listed = await srv.handle({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
        assert {t["name"] for t in listed["result"]["prompts"]} == {"review", "plan"}
        got = await srv.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "prompts/get",
                "params": {"name": "review", "arguments": {"code": "y"}},
            }
        )
        assert got["result"]["messages"][0]["role"] == "user"
        missing = await srv.handle({"jsonrpc": "2.0", "id": 4, "method": "prompts/get"})
        assert missing["error"]["code"] == -32602
        unknown = await srv.handle(
            {"jsonrpc": "2.0", "id": 5, "method": "prompts/get", "params": {"name": "nope"}}
        )
        assert unknown["error"]["code"] == -32602

    asyncio.run(_go())

    async def _bare():
        init = await MCPServer(Ikarem(enable_docs=False)).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
        assert "prompts" not in init["result"]["capabilities"]  # advertised only when present

    asyncio.run(_bare())


def test_custom_name_overrides_route_tool():
    app = Ikarem(enable_docs=False)

    @app.get("/thing")
    async def thing(req):
        return {"route": True}

    @app.tool("thing")
    def thing_fn():
        return {"custom": True}

    tools = {t["name"]: t for t in app.mcp_tools()}
    assert len([t for t in app.mcp_tools() if t["name"] == "thing"]) == 1

    async def _go():
        out = await app.mcp_call("thing", {})
        assert out["isError"] is False and out["content"][0]["text"] == '{"custom": true}'

    asyncio.run(_go())
    assert tools["thing"]["description"] == "thing"  # falls back to the tool name
