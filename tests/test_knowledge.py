"""knowledge.py: framework manual as MCP tools. Fast, no server needed."""

import asyncio

from ikarem import knowledge
from ikarem.mcp import MCPServer


def _srv():
    return MCPServer(knowledge.app)


def test_tools_listed():
    names = {t["name"] for t in _srv().list_tools()}
    assert {
        "ikarem_quickref",
        "ikarem_doc",
        "ikarem_search",
        "ikarem_api",
        "ikarem_example",
        "ikarem_audit",
    } <= names


def test_quickref_needs_no_checkout():
    out = asyncio.run(_srv().call_tool("ikarem_quickref", {}))
    assert not out["isError"] and "req FIRST" in out["content"][0]["text"]


def test_doc_manual_and_unknown_topic():
    srv = _srv()
    manual = asyncio.run(srv.call_tool("ikarem_doc", {}))
    assert not manual["isError"]
    bad = asyncio.run(srv.call_tool("ikarem_doc", {"topic": "nope"}))
    assert bad["isError"] and "Did you mean" in bad["content"][0]["text"]


def test_search_and_api():
    srv = _srv()
    hits = asyncio.run(srv.call_tool("ikarem_search", {"query": "readOnlyHint"}))
    assert not hits["isError"] and "mcp.py" in hits["content"][0]["text"]
    ref = asyncio.run(srv.call_tool("ikarem_api", {"symbol": "Depends"}))
    assert not ref["isError"] and "ikarem.Depends(" in ref["content"][0]["text"]
    missing = asyncio.run(srv.call_tool("ikarem_api", {"symbol": "Depend"}))
    assert missing["isError"] and "Did you mean" in missing["content"][0]["text"]


def test_audit_accepts_good_app():
    code = (
        "from ikarem import Ikarem\napp = Ikarem(enable_docs=False)\n"
        "@app.get('/hi')\nasync def hi(req):\n    return {'hi': 1}\n"
    )
    out = asyncio.run(_srv().call_tool("ikarem_audit", {"code": code}))
    text = out["content"][0]["text"]
    assert not out["isError"] and "GET /hi -> hi" in text and "check OK" in text


def test_audit_names_missing_app():
    out = asyncio.run(_srv().call_tool("ikarem_audit", {"code": "x = 1"}))
    assert "no Ikarem app" in out["content"][0]["text"]


def test_prompts_render():
    srv = _srv()
    assert [p["name"] for p in srv.list_prompts()] == ["ikarem_new_app", "ikarem_review"]
    got = asyncio.run(srv.get_prompt("ikarem_new_app", {"name": "demo"}))
    assert "demo" in got["messages"][0]["content"][0]["text"]
    missing = asyncio.run(srv.get_prompt("ikarem_review", {}))
    assert missing["isError"]
