"""SQLite persistence for sessions, messages, memories. Stdlib only."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_DB = Path(os.environ.get("HERMES_DB", Path(__file__).parent / "hermes.db"))


def db_path() -> str:
    return str(_DB)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT DEFAULT 'New chat',
                    created REAL DEFAULT 0,
                    updated REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    tool_trace TEXT DEFAULT '[]',
                    created REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE,
                    value TEXT,
                    updated REAL DEFAULT 0
                );
                """
            )
            conn.commit()
        finally:
            conn.close()


def new_session(title: str = "New chat") -> dict:
    init_db()
    sid = secrets.token_hex(8)
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO sessions (id, title, created, updated) VALUES (?, ?, ?, ?)",
                (sid, title, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return {"id": sid, "title": title, "created": now, "updated": now}


def list_sessions(limit: int = 50) -> list[dict]:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM sessions ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def touch_session(sid: str, title: str | None = None) -> None:
    init_db()
    now = time.time()
    conn = _connect()
    try:
        if title:
            conn.execute("UPDATE sessions SET updated = ?, title = ? WHERE id = ?", (now, title[:80], sid))
        else:
            conn.execute("UPDATE sessions SET updated = ? WHERE id = ?", (now, sid))
        if conn.total_changes == 0:
            conn.execute(
                "INSERT INTO sessions (id, title, created, updated) VALUES (?, ?, ?, ?)",
                (sid, title or "New chat", now, now),
            )
        conn.commit()
    finally:
        conn.close()


def delete_session(sid: str) -> None:
    init_db()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            conn.commit()
        finally:
            conn.close()


def add_message(sid: str, role: str, content: str, tool_trace: list | None = None) -> None:
    init_db()
    touch_session(sid)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_trace, created) VALUES (?, ?, ?, ?, ?)",
            (sid, role, content, json.dumps(tool_trace or []), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def get_history(sid: str, limit: int = 60) -> list[dict]:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (sid, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    finally:
        conn.close()


def get_full_log(sid: str, limit: int = 200) -> list[dict]:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT role, content, tool_trace, created FROM messages WHERE session_id = ?"
            " ORDER BY id ASC LIMIT ?",
            (sid, limit),
        ).fetchall()
        out = []
        for r in rows:
            try:
                trace = json.loads(r["tool_trace"] or "[]")
            except Exception:
                trace = []
            out.append(
                {"role": r["role"], "content": r["content"], "tool_trace": trace, "created": r["created"]}
            )
        return out
    finally:
        conn.close()


def remember(key: str, value: str) -> None:
    init_db()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO memories (key, value, updated) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated = excluded.updated",
                (key, value, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def recall(key: str = "") -> list[dict]:
    init_db()
    conn = _connect()
    try:
        if key:
            rows = conn.execute("SELECT key, value FROM memories WHERE key = ?", (key,)).fetchall()
        else:
            rows = conn.execute("SELECT key, value FROM memories ORDER BY updated DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def forget(key: str) -> bool:
    init_db()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM memories WHERE key = ?", (key,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
