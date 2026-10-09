"""BYOK LLM client. Stdlib only (urllib) - works with any OpenAI-compatible API.

Providers: openai, anthropic, openrouter, groq, gemini, ollama, lmstudio, custom.
Keys come from the caller (BYOK headers) or env fallback. Never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from typing import Any

PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "o4-mini"],
        "key_env": "OPENAI_API_KEY",
        "kind": "openai",
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-1"],
        "key_env": "ANTHROPIC_API_KEY",
        "kind": "anthropic",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4-5", "google/gemini-2.5-flash"],
        "key_env": "OPENROUTER_API_KEY",
        "kind": "openai",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"],
        "key_env": "GROQ_API_KEY",
        "kind": "openai",
    },
    "gemini": {
        "label": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-2.5-flash"],
        "key_env": "GEMINI_API_KEY",
        "kind": "openai",
    },
    "ollama": {
        "label": "Ollama (local, no key)",
        "base_url": "http://localhost:11434/v1",
        "models": ["llama3.1", "qwen2.5", "mistral"],
        "key_env": "",
        "kind": "openai",
    },
    "lmstudio": {
        "label": "LM Studio (local, no key)",
        "base_url": "http://localhost:1234/v1",
        "models": ["local-model"],
        "key_env": "",
        "kind": "openai",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "base_url": "",
        "models": ["custom-model"],
        "key_env": "",
        "kind": "openai",
    },
}

DEFAULT_MODEL = "gpt-4o-mini"


class LLMError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message + (f" Hint: {hint}" if hint else ""))
        self.hint = hint


def resolve_config(provider: str, model: str = "", base_url: str = "", api_key: str = "") -> dict[str, Any]:
    """Merge caller BYOK values over known-provider defaults + env fallback."""
    p = (provider or "openai").lower()
    meta = PROVIDERS.get(p, PROVIDERS["openai"])
    key = (api_key or "").strip()
    if not key and meta["key_env"]:
        key = os.environ.get(meta["key_env"], "").strip()
    url = (base_url or "").strip() or os.environ.get("HERMES_BASE_URL", "").strip() or meta["base_url"]
    mdl = (
        (model or "").strip()
        or os.environ.get("HERMES_MODEL", "").strip()
        or (meta["models"][0] if meta["models"] else DEFAULT_MODEL)
    )
    needs_key = p not in ("ollama", "lmstudio")
    if needs_key and not key:
        raise LLMError(
            f"No API key for provider '{p}'.",
            "Add your key in the UI (BYOK) or set "
            f"{meta['key_env'] or 'the key field'} - keys stay in your browser.",
        )
    if not url:
        raise LLMError(
            f"No base URL for provider '{p}'.", "Pick a provider or enter a custom Base URL in the UI."
        )
    return {
        "provider": p,
        "kind": meta["kind"],
        "base_url": url.rstrip("/"),
        "model": mdl,
        "api_key": key,
        "label": meta["label"],
    }


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 120.0) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:800]
        except Exception:
            detail = ""
        raise LLMError(
            f"LLM HTTP {e.code}: {detail or e.reason}", "Check key / model / base URL in the BYOK panel."
        )
    except urllib.error.URLError as e:
        raise LLMError(
            f"Cannot reach LLM endpoint: {e.reason}",
            "For local models, start Ollama (`ollama serve`) or LM Studio first.",
        )


def chat_openai(
    cfg: dict,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2000,
) -> dict:
    """One non-streaming chat-completions call. Returns normalized dict."""
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]
        payload["tool_choice"] = "auto"
    raw = _post_json(cfg["base_url"] + "/chat/completions", payload, headers)
    try:
        msg = raw["choices"][0]["message"]
    except (KeyError, IndexError):
        raise LLMError(
            f"Unexpected LLM response: {str(raw)[:400]}",
            "Try another model - this one may not speak OpenAI protocol.",
        )
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments", "")}
        calls.append({"id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": args})
    return {"content": msg.get("content") or "", "tool_calls": calls, "raw": raw}


def chat_anthropic(
    cfg: dict,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2000,
) -> dict:
    system, convo = "", []
    for m in messages:
        if m["role"] == "system":
            system += m["content"] + "\n"
        else:
            convo.append({"role": "user" if m["role"] == "tool" else m["role"], "content": m["content"]})
    headers = {
        "Content-Type": "application/json",
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
    }
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system or "You are helpful.",
        "messages": convo or [{"role": "user", "content": "hi"}],
    }
    if tools:
        payload["tools"] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {"type": "object"}),
            }
            for t in tools
        ]
    raw = _post_json(cfg["base_url"] + "/messages", payload, headers)
    text, calls = "", []
    for block in raw.get("content") or []:
        if block.get("type") == "text":
            text += block.get("text", "")
        elif block.get("type") == "tool_use":
            calls.append(
                {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": block.get("input", {}),
                }
            )
    return {"content": text, "tool_calls": calls, "raw": raw}


def chat(
    cfg: dict,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2000,
) -> dict:
    if cfg["kind"] == "anthropic":
        return chat_anthropic(cfg, messages, tools, temperature, max_tokens)
    return chat_openai(cfg, messages, tools, temperature, max_tokens)


async def achat(
    cfg: dict,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2000,
) -> dict:
    return await asyncio.to_thread(chat, cfg, messages, tools, temperature, max_tokens)


def provider_list() -> list[dict]:
    return [
        {
            "id": k,
            "label": v["label"],
            "models": v["models"],
            "needs_key": bool(v["key_env"]),
            "default_base_url": v["base_url"],
        }
        for k, v in PROVIDERS.items()
    ]
