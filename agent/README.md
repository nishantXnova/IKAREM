# HERMES - versatile BYOK AI agent on IKAREM

Like an OpenClaw-style command agent, running on the IKAREM framework in this repo.
Zero required dependencies (stdlib + `ikarem` core). Keys are **bring-your-own**:
they live in your browser and ride per-request headers - the server never stores them.

## Run

```bash
pip install -e ".[dev]"
ikarem run agent.app:app        # UI at http://127.0.0.1:8000/
```

No key? Use **Ollama** (local, keyless): `ollama serve && ollama pull llama3.1`,
then pick provider `ollama` in the key panel. Or paste any OpenAI / Anthropic /
OpenRouter / Groq / Gemini key, or point Base URL at LM Studio / a gateway.

## What it does

- **Chat with tools (ReAct loop):** files (`list_dir/read/write/edit`), allowlisted
  `shell`, `web_fetch`, durable `memory` - streamed over `/ws/chat` with a live
  tool trace. Writes/shell/memory ask **approval first** (`confirm=true`).
- **Workspace jail:** everything stays under `agent/workspace/`. Shell is
  allowlisted, deny-patterned (`rm -rf /`, fork bombs...), and time-boxed.
- **Sessions + memories** in SQLite (`agent/hermes.db`).
- **MCP inside:** every route is already an MCP tool, plus extras:
  `hermes_chat`, `hermes_read_file`, `hermes_list_dir`, `hermes_web_fetch`,
  `hermes_recall`, and prompts `hermes_system` / `hermes_plan`.

```bash
ikarem mcp agent.app:app --list
ikarem check agent.app:app
```

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/` | chat UI |
| GET | `/api/providers` | provider catalogue + server-key presence |
| POST | `/api/chat` | `{message, session_id, max_steps}` - headers `X-Provider/X-Model/X-Base-Url/X-Api-Key` |
| WS | `/ws/chat` | first frame `{message, session_id, provider, model, base_url, api_key}` |
| GET/POST/DELETE | `/api/sessions` | chat history |
| GET | `/api/files?path=` `/api/file?path=` | workspace browse |
| POST | `/api/tools/call` | `{name, arguments}` direct dispatch |
| GET/POST | `/api/memories` | durable memory (writes need `confirm:true`) |

## Security posture (local-first, stated plainly)

- No auth on the API: bind to loopback (`ikarem run` defaults to
  127.0.0.1) and never expose HERMES to a network. Writes/shell/memory
  still ask approval per call (`confirm=true`).
- `ikarem check` lists the public mutating routes as warnings — that is
  the documented posture above, not an oversight.
- Sessions sign with `HERMES_SESSION_SECRET` (dev default warns at
  startup); set it before any non-local use. Keys stay BYOK and are
  never persisted.

## Layout

```
agent/app.py        Ikarem app: UI + API + WS + MCP tools/prompts
agent/llm.py        BYOK client (OpenAI-protocol + Anthropic native, urllib only)
agent/loop.py       ReAct loop + charter system prompt
agent/tools.py      toolbelt + jail + allowlist + JSON schemas
agent/store.py      SQLite sessions/messages/memories
agent/static/       polished dark-glass UI (no build step)
agent/workspace/    the agent playground (jailed)
agent/tests/        suite (runs inside repo pytest)
```
