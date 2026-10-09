"""HERMES agent tests - run with the repo suite."""

import pytest

from agent import llm as _llm
from agent import loop as _loop
from agent import store as _store
from agent import tools as _tools
from agent.app import app
from ikarem.testing import TestClient


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Tests never touch the dev database: every store call reads the
    module-global path, so one monkeypatch isolates the whole suite."""
    monkeypatch.setattr(_store, "_DB", tmp_path / "test-hermes.db")


def _client():
    return TestClient(app)


def test_index_serves_ui():
    c = _client()
    r = c.get("/")
    assert r.status_code == 200
    assert "HERMES" in r.text
    assert "byokModal" in r.text


def test_providers_list():
    c = _client()
    r = c.get("/api/providers")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["providers"]]
    for expected in ("openai", "anthropic", "ollama", "custom"):
        assert expected in ids


def test_chat_needs_key_without_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    c = _client()
    r = c.post("/api/chat", body={"message": "hi", "session_id": "t-need-key"})
    assert r.status_code == 428
    assert r.json()["need_key"] is True


def test_sessions_roundtrip():
    c = _client()
    created = c.post("/api/sessions", body={"title": "hello"}).json()
    assert created["id"]
    lst = c.get("/api/sessions").json()["sessions"]
    assert any(s["id"] == created["id"] for s in lst)
    log = c.get(f"/api/sessions/{created['id']}").json()
    assert log["messages"] == []
    assert c.delete(f"/api/sessions/{created['id']}").json() == {"ok": True}


def test_tool_gating_and_jail():
    gated = _tools.dispatch("write_file", {"path": "x.txt", "content": "hi"})
    assert gated["needs_confirm"] is True
    try:
        _tools._jail("../../evil")
    except ValueError as e:
        assert "escapes workspace" in str(e)
    else:
        raise AssertionError("jail must refuse ../..")


def test_shell_allowlist():
    refused = _tools.dispatch("shell", {"command": "rm -rf /", "confirm": True})
    assert "error" in refused
    gated = _tools.dispatch("shell", {"command": "echo hi"})
    assert gated["needs_confirm"] is True


def test_llm_resolve_byok_and_errors():
    cfg = _llm.resolve_config("ollama", "llama3.1", "", "")
    assert cfg["base_url"].startswith("http://localhost")
    try:
        _llm.resolve_config("openai", "gpt-4o-mini", "", "")
    except _llm.LLMError as e:
        assert "BYOK" in str(e) or "API key" in str(e)
    else:
        raise AssertionError("openai without key must raise with a remedy")


def test_loop_builds_messages_with_system_first():
    msgs = _loop.build_messages(
        [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], "do it"
    )
    assert msgs[0]["role"] == "system" and "HERMES" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "do it"}


def test_tools_call_route_lists_workspace():
    c = _client()
    r = c.post("/api/tools/call", body={"name": "list_dir", "arguments": {"path": "."}})
    assert r.status_code == 200
    assert "items" in r.json()["result"] or "error" in r.json()["result"]


def test_mcp_tools_registered():
    server = app.mcp_server().build()
    names = [t["name"] for t in server.list_tools()]
    assert "hermes_chat" in names and "hermes_read_file" in names
    prompts = [p["name"] for p in server.list_prompts()]
    assert "hermes_system" in prompts and "hermes_plan" in prompts
