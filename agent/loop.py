"""ReAct agent loop: system prompt + tool calls + confirmation gating."""

from __future__ import annotations

import json
from typing import Any, Callable

from . import llm as _llm
from . import tools as _tools

SYSTEM = """You are HERMES, a versatile AI agent running inside the IKAREM framework.
Be concise, factual, and action-oriented - like a senior engineer pair-programming.

Rules:
- Orient first: list_dir before reading; read before editing.
- write_file / edit_file / shell / remember need confirm=true. Without user
  confirmation the tool returns needs_confirm - explain what you WILL do and
  ask for confirmation instead of retrying blindly.
- Prefer small exact edits (edit_file) over full rewrites.
- Shell is allowlisted and workspace-jailed; never emit destructive commands.
- If no API key is configured the server tells you - guide the user to the
  BYOK panel (OpenAI / Anthropic / OpenRouter / Groq / Gemini / Ollama local).
- Errors must say how to fix. Never claim you did what a tool refused.
- Final answers: short summary + files touched + how to verify.
"""


def build_messages(history: list[dict], task: str) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": SYSTEM}]
    msgs += [
        {"role": m["role"], "content": m["content"]}
        for m in history[-30:]
        if m.get("role") in ("user", "assistant")
    ]
    msgs.append({"role": "user", "content": task})
    return msgs


async def run(
    task: str,
    history: list[dict],
    llm_cfg: dict,
    max_steps: int = 8,
    on_event: Callable[[dict], Any] | None = None,
) -> dict:
    """Run the loop. Events: thought | tool_call | tool_result | done | error."""

    async def emit(ev: dict) -> None:
        if on_event is not None:
            r = on_event(ev)
            if hasattr(r, "__await__"):
                await r

    messages = build_messages(history, task)
    trace: list[dict] = []
    for step in range(max_steps):
        try:
            out = await _llm.achat(llm_cfg, messages, _tools.DEFS)
        except _llm.LLMError as e:
            await emit({"type": "error", "message": str(e)})
            return {"answer": str(e), "trace": trace, "steps": step}
        content, calls = out.get("content", ""), out.get("tool_calls", [])
        if content:
            await emit({"type": "thought", "text": content, "step": step})
        if not calls:
            await emit({"type": "done", "text": content})
            return {"answer": content or "(no answer)", "trace": trace, "steps": step + 1}
        # One assistant turn, then one follow-up message per tool result.
        if all(c.get("id") for c in calls):
            messages.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                        }
                        for c in calls
                    ],
                }
            )
        else:
            messages.append(
                {
                    "role": "assistant",
                    "content": content or f"(calling {', '.join(c['name'] for c in calls)})",
                }
            )
        for call in calls:
            await emit(
                {"type": "tool_call", "name": call["name"], "arguments": call["arguments"], "step": step}
            )
            result = _tools.dispatch(call["name"], call["arguments"])
            trace.append({"tool": call["name"], "args": call["arguments"], "result": result})
            await emit({"type": "tool_result", "name": call["name"], "result": result})
            if call.get("id"):
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": _tools.result_text(result)}
                )
            else:
                messages.append(
                    {"role": "user", "content": f"[tool {call['name']} result] {_tools.result_text(result)}"}
                )
    await emit({"type": "done", "text": "(stopped after max steps)"})
    return {"answer": "Stopped after max steps - try a smaller task.", "trace": trace, "steps": max_steps}
