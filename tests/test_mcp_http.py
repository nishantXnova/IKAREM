"""MCP over Streamable HTTP: POST round-trips, SSE shape, statelessness."""

from ikarem import Ikarem
from ikarem.testing import TestClient

CT = "application/json"


def _app():
    app = Ikarem(enable_docs=False)

    @app.tool()
    def add(a: int, b: int = 1):
        """Add two numbers."""
        return a + b

    @app.get("/users/{uid:int}")
    async def get_user(req, uid: int):
        return {"uid": uid}

    app.mount_mcp("/mcp")
    return app


def _post(c, payload, **kw):
    import json as _json

    body = payload if isinstance(payload, bytes) else _json.dumps(payload).encode()
    return c.post("/mcp", body=body, content_type=kw.pop("content_type", CT), **kw)


def test_initialize_and_tools_over_http():
    c = TestClient(_app())
    init = _post(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init.status_code == 200
    body = init.json()
    assert body["result"]["protocolVersion"] == "2024-11-05"
    assert "mcp-session-id" not in {k.lower() for k in init.headers}  # stateless: no sessions
    listed = _post(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in listed.json()["result"]["tools"]} >= {"add", "get_user"}


def test_call_route_and_custom_tool_over_http():
    c = TestClient(_app())
    out = _post(
        c,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 2}}},
    )
    assert out.json()["result"]["isError"] is False
    assert out.json()["result"]["content"][0]["text"] == "3"
    user = _post(
        c,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_user", "arguments": {"uid": 9}},
        },
    )
    assert user.json()["result"]["content"][0]["text"] == '{"uid": 9}'


def test_protocol_errors_ride_http_200_batch_and_notifications():
    c = TestClient(_app())
    unknown = _post(c, {"jsonrpc": "2.0", "id": 5, "method": "nope"})
    assert unknown.status_code == 200  # JSON-RPC error, not HTTP error
    assert unknown.json()["error"]["code"] == -32601
    batch = _post(
        c,
        [
            {"jsonrpc": "2.0", "id": 6, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ],
    )
    assert batch.status_code == 200
    assert [m["id"] for m in batch.json()] == [6]
    only_notify = _post(c, [{"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert only_notify.status_code == 202 and only_notify.body == b""


def test_malformed_envelopes_rejected():
    c = TestClient(_app())
    bad_ct = c.post("/mcp", body=b"{}", content_type="text/plain")
    assert bad_ct.status_code == 415
    bad_json = c.post("/mcp", body=b"{oops", content_type=CT)
    assert bad_json.status_code == 400
    assert bad_json.json()["error"]["code"] == -32700
    bad_rpc = _post(c, {"nope": True})
    assert bad_rpc.status_code == 200
    assert bad_rpc.json()["error"]["code"] == -32600


def test_sse_stream_shape_and_internal_routes_hidden():
    c = TestClient(_app())
    stream = c.get("/mcp")
    assert stream.status_code == 200
    assert stream.headers["content-type"] == "text/event-stream"
    assert b"connected" in stream.body
    names = [t["name"] for t in _app().mcp_tools()]
    assert not any("mcp" in n for n in names)  # transport routes are not tools
