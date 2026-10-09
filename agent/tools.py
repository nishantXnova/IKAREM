"""Agent toolbelt: files, shell, web, memory. Stdlib only, workspace-jained.

Safety: read/list/recall run freely. write/edit/shell/memory-write need
explicit `confirm=true` from the caller (the UI asks the user first).
Shell is allowlisted and time-boxed; everything stays under WORKSPACE.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).parent / "workspace"
WORKSPACE.mkdir(exist_ok=True)

SHELL_ALLOW = (
    "ls",
    "dir",
    "echo",
    "cat",
    "type",
    "pwd",
    "cd",
    "python",
    "pip",
    "pytest",
    "ruff",
    "git",
    "node",
    "npm",
    "uvicorn",
    "ikarem",
)
SHELL_DENY = re.compile(r"(rm\s+-rf\s+/( |$)|:\(\)\s*\{|mkfs|shutdown|reboot|format\s+[a-z]:)", re.IGNORECASE)
MAX_OUTPUT = 12_000


def _jail(rel: str) -> Path:
    p = (WORKSPACE / (rel or ".")).resolve()
    if p != WORKSPACE.resolve() and WORKSPACE.resolve() not in p.parents:
        raise ValueError(f"Path escapes workspace: {rel!r}. Stay under workspace/.")
    return p


def tool_list_dir(path: str = ".") -> dict:
    p = _jail(path)
    if not p.exists():
        return {"error": f"not found: {path}"}
    if p.is_file():
        return {"path": path, "type": "file", "size": p.stat().st_size}
    items = []
    for child in sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))[:200]:
        items.append(
            {
                "name": child.name,
                "type": "file" if child.is_file() else "dir",
                "size": child.stat().st_size if child.is_file() else 0,
            }
        )
    return {"path": path, "items": items}


def tool_read_file(path: str, limit: int = 200) -> dict:
    p = _jail(path)
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    if p.stat().st_size > 1_000_000:
        return {"error": "file too large (>1MB). Refine the path."}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"cannot read: {e}"}
    lines = text.splitlines()
    if len(lines) > limit:
        return {
            "path": path,
            "truncated": True,
            "content": "\n".join(lines[:limit]),
            "note": f"showing {limit}/{len(lines)} lines",
        }
    return {"path": path, "content": text}


def tool_write_file(path: str, content: str, confirm: bool = False) -> dict:
    if not confirm:
        return {
            "needs_confirm": True,
            "message": f"Writing '{path}' needs confirm=true. The UI asks you first.",
        }
    p = _jail(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "bytes": len(content.encode())}


def tool_edit_file(path: str, old: str, new: str, confirm: bool = False) -> dict:
    if not confirm:
        return {
            "needs_confirm": True,
            "message": f"Editing '{path}' needs confirm=true. The UI asks you first.",
        }
    p = _jail(path)
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    text = p.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        matches = [ln for ln in text.splitlines() if old.strip()[:40] in ln][:5] if old.strip() else []
        return {
            "error": "old string not found",
            "hint_lines": matches,
            "fix": "Read the file first, then copy the exact block including whitespace.",
        }
    if text.count(old) > 1:
        return {
            "error": f"old string matches {text.count(old)} times",
            "fix": "Include more surrounding lines to make it unique.",
        }
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return {"ok": True, "path": path}


def tool_shell(command: str, confirm: bool = False, timeout: int = 30) -> dict:
    if not confirm:
        return {"needs_confirm": True, "message": "Shell needs confirm=true. The UI asks you first."}
    cmd = (command or "").strip()
    if not cmd:
        return {"error": "empty command"}
    if SHELL_DENY.search(cmd):
        return {"error": "refused: destructive pattern blocked"}
    first = cmd.split()[0].lower().rstrip(".exe")
    if first not in SHELL_ALLOW and not cmd.startswith(("python ", "python3 ")):
        return {
            "error": f"refused: '{first}' not on the allowlist",
            "fix": f"Allowed: {', '.join(SHELL_ALLOW)}. Ask to extend it.",
        }
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=min(timeout, 60), cwd=str(WORKSPACE)
        )
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s", "fix": "Use a faster command."}
    out = (proc.stdout or "") + (proc.stderr or "")
    return {
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "output": out[:MAX_OUTPUT] + ("...[truncated]" if len(out) > MAX_OUTPUT else ""),
    }


def tool_web_fetch(url: str) -> dict:
    if not url.startswith(("http://", "https://")):
        return {"error": "URL must start with http:// or https://"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HERMES-agent/0.1"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()[:60_000].decode(errors="replace")
    except Exception as e:
        return {"error": f"fetch failed: {e}", "fix": "Check the URL; the server reads public pages only."}
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return {"url": url, "text": text[:MAX_OUTPUT]}


# ---- LLM-facing JSON schemas (OpenAI function style) ----

DEFS: list[dict] = [
    {
        "name": "list_dir",
        "description": "List workspace files. Start here to orient before reading.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "relative dir"}},
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "Read a workspace file (truncated past 200 lines).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer", "default": 200}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create/overwrite a workspace file. Needs confirm=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Exact-string replace in a file. Needs confirm=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "shell",
        "description": "Run an allowlisted shell command in workspace/. Needs confirm=true.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}, "confirm": {"type": "boolean", "default": False}},
            "required": ["command"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch a public URL and return readable text.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "remember",
        "description": "Save a durable memory (key/value) across chats.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall",
        "description": "Recall durable memories (all, or one key).",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string", "default": ""}},
            "required": [],
        },
    },
]


def dispatch(name: str, args: dict) -> dict:
    from . import store as _store

    try:
        if name == "list_dir":
            return tool_list_dir(args.get("path", "."))
        if name == "read_file":
            return tool_read_file(args.get("path", ""), int(args.get("limit", 200) or 200))
        if name == "write_file":
            return tool_write_file(
                args.get("path", ""), args.get("content", ""), bool(args.get("confirm", False))
            )
        if name == "edit_file":
            return tool_edit_file(
                args.get("path", ""),
                args.get("old", ""),
                args.get("new", ""),
                bool(args.get("confirm", False)),
            )
        if name == "shell":
            return tool_shell(args.get("command", ""), bool(args.get("confirm", False)))
        if name == "web_fetch":
            return tool_web_fetch(args.get("url", ""))
        if name == "remember":
            if not args.get("confirm", False):
                return {"needs_confirm": True, "message": "remember needs confirm=true."}
            _store.remember(args.get("key", ""), args.get("value", ""))
            return {"ok": True}
        if name == "recall":
            return {"memories": _store.recall(args.get("key", ""))}
        return {
            "error": f"unknown tool '{name}'",
            "fix": f"Use one of: {', '.join(t['name'] for t in DEFS)}.",
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def result_text(result: dict) -> str:
    try:
        return json.dumps(result)[:4000]
    except Exception:
        return str(result)[:4000]
