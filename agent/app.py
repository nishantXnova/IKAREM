"""HERMES - versatile BYOK AI agent on IKAREM.

Run:  ikarem run agent.app:app   (or uvicorn agent.app:app)
MCP:  ikarem mcp agent.app:app --list   (routes + tools + prompts)

UI at GET / - dark glass chat, BYOK modal, streaming via /ws/chat,
tool-approval cards, session sidebar. Keys live in the browser
(localStorage) and travel per-request; the server never persists them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ikarem import (
    CORSMiddleware,
    Ikarem,
    RateLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    SessionMiddleware,
)
from ikarem.http import HTMLResponse, JSONResponse
from ikarem.websocket import WebSocket, WebSocketDisconnect

from . import llm as _llm
from . import loop as _loop
from . import store as _store
from . import tools as _tools

BASE = Path(__file__).parent

app = Ikarem(session_secret=os.environ.get("HERMES_SESSION_SECRET", "hermes-dev-secret-change-me"))
app.use(RequestIDMiddleware())
app.use(SecurityHeadersMiddleware())
app.use(CORSMiddleware())
app.use(SessionMiddleware())
app.use(RateLimitMiddleware(per_minute=240))
app.mount_static("/static", str(BASE / "static"))

_store.init_db()
_tools.WORKSPACE.mkdir(exist_ok=True)


async def _secret_guard() -> None:
    if os.environ.get("HERMES_SESSION_SECRET") is None:
        import warnings

        warnings.warn(
            "HERMES running with the dev session secret: set HERMES_SESSION_SECRET before exposing this app"
        )


app.on_startup(_secret_guard)


# ---------------------------------------------------------------- helpers


def _byok_from(req) -> dict:
    """Provider config from headers (BYOK) with env fallback. Never logged."""
    h = req.headers
    return {
        "provider": h.get("x-provider", "") or "openai",
        "model": h.get("x-model", "") or "",
        "base_url": h.get("x-base-url", "") or "",
        "api_key": h.get("x-api-key", "") or "",
    }


def _llm_cfg_or_401(byok: dict):
    try:
        return _llm.resolve_config(byok["provider"], byok["model"], byok["base_url"], byok["api_key"]), None
    except _llm.LLMError as e:
        return None, {
            "error": str(e),
            "need_key": True,
            "fix": "Open the key icon (BYOK) and add a provider key, or use Ollama locally - no key needed.",
        }


# ---------------------------------------------------------------- UI


@app.get("/")
async def index(req):
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ---------------------------------------------------------------- providers (BYOK)


@app.get("/api/providers")
async def providers(req):
    items = _llm.provider_list()
    for it in items:
        meta = _llm.PROVIDERS[it["id"]]
        it["env_has_key"] = bool(meta["key_env"] and os.environ.get(meta["key_env"]))
    return {"providers": items}


# ---------------------------------------------------------------- sessions + history


@app.get("/api/sessions")
async def sessions_list(req):
    return {"sessions": _store.list_sessions()}


@app.post("/api/sessions")
async def sessions_new(req):
    body = await req.json() if "json" in req.headers.get("content-type", "") else {}
    title = str((body or {}).get("title", "New chat"))[:80]
    return _store.new_session(title), 201


@app.delete("/api/sessions/{sid}")
async def sessions_delete(req, sid: str):
    _store.delete_session(sid)
    return {"ok": True}


@app.get("/api/sessions/{sid}")
async def session_log(req, sid: str):
    return {"session": sid, "messages": _store.get_full_log(sid)}


# ---------------------------------------------------------------- chat (non-streaming)


@app.post("/api/chat")
async def chat(req):
    body = await req.json()
    task = str((body or {}).get("message", "")).strip()
    if not task:
        return JSONResponse(
            {"error": "message is required", "fix": "Send {message, session_id} as JSON."}, status_code=400
        )
    sid = str((body or {}).get("session_id", "default"))[:64] or "default"
    cfg, err = _llm_cfg_or_401(_byok_from(req))
    if err:
        return JSONResponse(err, status_code=428)
    history = _store.get_history(sid)
    _store.add_message(sid, "user", task)
    if len(task.split()) > 9:
        _store.touch_session(sid, " ".join(task.split()[:7]))
    result = await _loop.run(task, history, cfg, max_steps=int((body or {}).get("max_steps", 8) or 8))
    _store.add_message(sid, "assistant", result["answer"], result["trace"])
    return {
        "answer": result["answer"],
        "trace": result["trace"],
        "steps": result["steps"],
        "session_id": sid,
        "model": cfg["model"],
        "provider": cfg["provider"],
    }


# ---------------------------------------------------------------- files + tools + memory


@app.get("/api/files")
async def files(req):
    return _tools.tool_list_dir(req.query.get("path", "."))


@app.get("/api/file")
async def file_read(req):
    path = req.query.get("path", "")
    if not path:
        return JSONResponse({"error": "path query param is required"}, status_code=400)
    return _tools.tool_read_file(path, int(req.query.get("limit", "200") or 200))


@app.post("/api/tools/call")
async def tool_call(req):
    body = await req.json()
    name = str((body or {}).get("name", ""))
    args = (body or {}).get("arguments", {}) or {}
    if not name:
        return JSONResponse({"error": "tool name is required"}, status_code=400)
    return {"tool": name, "result": _tools.dispatch(name, args)}


@app.get("/api/memories")
async def memories_list(req):
    return {"memories": _store.recall(req.query.get("key", ""))}


@app.post("/api/memories")
async def memories_add(req):
    body = await req.json()
    key, value = str((body or {}).get("key", "")), str((body or {}).get("value", ""))
    if not key or not value:
        return JSONResponse({"error": "key and value are required"}, status_code=400)
    if not (body or {}).get("confirm"):
        return {"needs_confirm": True, "message": "memory write needs confirm=true"}
    _store.remember(key, value)
    return {"ok": True}


# ---------------------------------------------------------------- websocket streaming


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    try:
        raw = await ws.receive_text()
    except WebSocketDisconnect:
        return
    try:
        first = json.loads(raw or "{}")
    except Exception:
        first = {"message": raw}
    task = str(first.get("message", "")).strip()
    sid = str(first.get("session_id", "default"))[:64] or "default"
    byok = {
        "provider": str(first.get("provider", "openai")),
        "model": str(first.get("model", "")),
        "base_url": str(first.get("base_url", "")),
        "api_key": str(first.get("api_key", "")),
    }
    try:
        cfg = _llm.resolve_config(byok["provider"], byok["model"], byok["base_url"], byok["api_key"])
    except _llm.LLMError as e:
        await ws.send_json({"type": "error", "message": str(e), "need_key": True})
        return
    _store.add_message(sid, "user", task)
    history = _store.get_history(sid)

    async def emit(ev: dict) -> None:
        try:
            await ws.send_json(ev)
        except Exception:
            pass

    try:
        result = await _loop.run(task, history, cfg, on_event=emit)
    except Exception as e:
        await emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        return
    _store.add_message(sid, "assistant", result["answer"], result["trace"])
    await emit({"type": "final", "answer": result["answer"], "trace": result["trace"], "session_id": sid})


# ---------------------------------------------------------------- MCP: tools + prompts


@app.tool("hermes_list_dir")
async def _mcp_list_dir(path: str = ".") -> str:
    """List HERMES workspace files (orientation first)."""
    return json.dumps(_tools.tool_list_dir(path))


@app.tool("hermes_read_file")
async def _mcp_read_file(path: str) -> str:
    """Read a HERMES workspace file."""
    return json.dumps(_tools.tool_read_file(path))


@app.tool("hermes_web_fetch")
async def _mcp_web_fetch(url: str) -> str:
    """Fetch a public URL as readable text."""
    return json.dumps(_tools.tool_web_fetch(url))


@app.tool("hermes_recall")
async def _mcp_recall(key: str = "") -> str:
    """Recall HERMES durable memories."""
    return json.dumps({"memories": _store.recall(key)})


@app.tool("hermes_chat")
async def _mcp_chat(message: str, session_id: str = "mcp") -> str:
    """Ask HERMES (server-side model/env key) - runs the agent loop once."""
    try:
        cfg = _llm.resolve_config(
            os.environ.get("HERMES_PROVIDER", "openai"),
            os.environ.get("HERMES_MODEL", ""),
            os.environ.get("HERMES_BASE_URL", ""),
            "",
        )
    except _llm.LLMError as e:
        return json.dumps({"error": str(e)})
    result = await _loop.run(message, _store.get_history(session_id), cfg)
    _store.add_message(session_id, "user", message)
    _store.add_message(session_id, "assistant", result["answer"], result["trace"])
    return json.dumps({"answer": result["answer"], "trace": result["trace"]})


@app.prompt("hermes_system")
def _mcp_system() -> str:
    """HERMES system prompt - the agent charter."""
    return _loop.SYSTEM


@app.prompt("hermes_plan")
def _mcp_plan(goal: str) -> str:
    """Plan a task the HERMES way: orient, act small, verify."""
    return (
        f"Goal: {goal}\nPlan: 1) list_dir to orient 2) read the smallest relevant"
        " files 3) propose exact edits 4) verify with tests or a shell command."
    )
