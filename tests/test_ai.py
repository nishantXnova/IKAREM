"""AI surface: manifest, MCP resources, inspect CLI, llms.txt."""

import asyncio
import json
import pathlib

from ikarem import Depends, Field, Ikarem, Schema, require_roles
from ikarem.compiled import describe_app
from ikarem.mcp import MCPServer


def build_app():
    app = Ikarem(enable_docs=False, auth_secret="s")

    def pager(limit: int = 5):
        return limit

    class Item(Schema):
        name: str
        qty: int = Field(1, ge=1)

    @app.get("/users/{uid:int}")
    async def get_user(uid: int, limit=Depends(pager)):
        """Fetch one user."""
        return {"uid": uid}

    @app.post("/items")
    async def create_item(item: Item):
        return {"ok": True}

    @app.get("/admin")
    async def admin(req, claims=Depends(require_roles("admin"))):
        return {"ok": True}

    return app


def test_manifest_shape_and_compactness():
    m = describe_app(build_app())
    assert m["count"] == 3
    by_path = {(r["method"], r["path"]): r for r in m["routes"]}
    u = by_path[("GET", "/users/{uid:int}")]
    assert u["handler"] == "get_user"
    assert u["summary"] == "Fetch one user."
    assert u["path_params"] == [{"name": "uid", "type": "integer"}]
    assert {"name": "limit", "type": "integer", "required": False} in u["query"]
    assert "auth" not in u  # nulls omitted
    item = by_path[("POST", "/items")]
    assert item["body"]["required"] == ["name"]
    assert item["body"]["properties"]["qty"] == {"type": "integer", "minimum": 1}
    adm = by_path[("GET", "/admin")]
    assert adm["auth"] == {"scheme": "bearer", "roles": ["admin"]}
    assert len(json.dumps(m)) < 1500  # token-efficient by construction


def test_mcp_resources_roundtrip():
    srv = MCPServer(build_app())

    async def go():
        listed = await srv.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
        uris = {t["uri"] for t in listed["result"]["resources"]}
        assert uris == {"ikarem://openapi.json", "ikarem://manifest"}
        spec = await srv.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "ikarem://openapi.json"},
            }
        )
        assert json.loads(spec["result"]["contents"][0]["text"])["openapi"].startswith("3.1")
        man = await srv.handle(
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ikarem://manifest"}}
        )
        assert json.loads(man["result"]["contents"][0]["text"])["count"] == 3
        bad = await srv.handle(
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "ikarem://nope"}}
        )
        assert bad["result"]["isError"] is True
        missing = await srv.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {}})
        assert missing["error"]["code"] == -32602

    asyncio.run(go())


def test_inspect_cli_json_and_summary(capsys, monkeypatch):

    import pytest

    import tests.test_ai as selfmod
    from ikarem import cli

    selfmod._app = build_app()
    monkeypatch.setattr("sys.argv", ["ikarem", "inspect", "tests.test_ai:_app"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["count"] == 3
    assert "GET" in out and "/users/{uid:int}" in out


_app = None  # placeholder so `tests.test_ai:_app` resolves if referenced


def test_llms_txt_exists_and_covers_api():
    llms = pathlib.Path(__file__).parent.parent / "site" / "llms.txt"
    assert llms.exists(), "site/llms.txt missing"
    text = llms.read_text(encoding="utf-8")
    for token in [
        "pip install ikarem",
        "Depends",
        "Schema",
        "require_roles",
        "DatabasePlugin",
        "TestClient",
        "ikarem inspect",
        "ikarem mcp",
        "QueuePlugin",
        "never f-string SQL",
    ]:
        assert token in text, f"llms.txt missing: {token}"
    assert len(text) < 9000  # stays a quick read, not a dump
