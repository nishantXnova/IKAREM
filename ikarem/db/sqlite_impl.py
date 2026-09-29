"""SQLite implementation — stdlib only, works out of the box.

Uses sqlite3 in a threadpool via asyncio.to_thread so the event loop
never blocks. `?` placeholders natively; accepts both `?` and `%s`/`$1`
style by normalizing to `?` for DX parity across engines.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Any
from urllib.parse import urlparse

from .base import DatabaseConnector, Transaction


def _normalize(query: str) -> str:
    query = re.sub(r"%\w", "?", query)
    query = re.sub(r"\$\d+", "?", query)
    return query


class _Tx(Transaction):
    def __init__(self, conn: "SQLiteConnector"):
        self._c = conn

    async def __aenter__(self) -> "_Tx":
        # No explicit BEGIN: pysqlite opens one implicitly on first DML.
        # The depth flag only suppresses per-statement auto-commit so the
        # whole block commits or rolls back atomically.
        self._c._tx_depth += 1
        return self

    async def commit(self) -> None:
        def _op() -> None:
            with self._c._tlock:
                self._c._ensure().commit()
                self._c._tx_depth = max(0, self._c._tx_depth - 1)

        await asyncio.to_thread(_op)

    async def rollback(self) -> None:
        def _op() -> None:
            with self._c._tlock:
                self._c._ensure().rollback()
                self._c._tx_depth = max(0, self._c._tx_depth - 1)

        await asyncio.to_thread(_op)


class SQLiteConnector(DatabaseConnector):
    dialect = "sqlite"

    def __init__(self, url: str = "sqlite:///:memory:", **options: Any):
        super().__init__(url, **options)
        parsed = urlparse(url)
        path = parsed.path if parsed.path not in ("", "/") else "/:memory:"
        if path.startswith("/") and not path.startswith("//") and parsed.scheme == "sqlite":
            # sqlite:///app.db -> /app.db ; sqlite:///:memory: stays
            path = path[1:] if not path == "/:memory:" else ":memory:"
        if url.endswith(":memory:"):
            path = ":memory:"
        self.path = path or ":memory:"
        self._conn: sqlite3.Connection | None = None
        self._tx_depth = 0
        # One shared connection (keeps :memory: working) means every use
        # must be serialized: pysqlite connections corrupt under concurrent
        # use. The lock lives INSIDE the worker threads (threading.Lock, not
        # asyncio.Lock): asyncio locks bind to one event loop and break
        # TestClient-style per-request loops, while a threading.Lock gives
        # true mutual exclusion across loops and threads without ever
        # blocking the event loop. Documented single-lane tradeoff of the
        # SQLite strategy — Postgres pools when you outgrow it.
        import threading as _th

        self._tlock = _th.Lock()

    async def connect(self) -> None:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # WAL: readers never block writers and vice versa. Writers still
            # serialize (single-writer engine), but reads stay fast under load.
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass  # :memory: and exotic mounts stay on rollback journal
            return conn

        self._conn = await asyncio.to_thread(_open)
        self.connected = True

    async def disconnect(self) -> None:
        if self._conn:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
        self.connected = False

    def _ensure(self) -> sqlite3.Connection:
        assert self._conn is not None, "Not connected. Call await connect() first."
        return self._conn

    async def execute(self, query: str, *params: Any) -> Any:
        def _op() -> int:
            with self._tlock:
                cur = self._ensure().execute(_normalize(query), params)
                if self._tx_depth == 0:
                    self._ensure().commit()
                return cur.lastrowid if cur.lastrowid is not None else cur.rowcount

        return await asyncio.to_thread(_op)

    async def fetch_one(self, query: str, *params: Any) -> dict | None:
        def _op() -> dict | None:
            with self._tlock:
                cur = self._ensure().execute(_normalize(query), params)
                row = cur.fetchone()
                return dict(row) if row else None

        return await asyncio.to_thread(_op)

    async def fetch_all(self, query: str, *params: Any) -> list[dict]:
        def _op() -> list[dict]:
            with self._tlock:
                cur = self._ensure().execute(_normalize(query), params)
                return [dict(r) for r in cur.fetchall()]

        return await asyncio.to_thread(_op)

    def transaction(self) -> Transaction:
        return _Tx(self)
