"""PostgreSQL strategy — lazy asyncpg import, core stays light."""

from __future__ import annotations

from typing import Any

from .base import DatabaseConnector, Transaction


class _Tx(Transaction):
    def __init__(self, conn: "PostgresConnector"):
        self._c = conn
        self._tx: Any = None

    async def _begin(self) -> None:
        assert self._c._pool is not None, "Not connected"
        c = await self._c._pool.acquire()
        self._tx = c.transaction()
        await self._tx.start()
        self._c._tx_conn = c

    async def commit(self) -> None:
        if self._tx is not None:
            await self._tx.commit()
            await self._c._pool.release(self._c._tx_conn)
            self._tx, self._c._tx_conn = None, None

    async def rollback(self) -> None:
        if self._tx is not None:
            await self._tx.rollback()
            await self._c._pool.release(self._c._tx_conn)
            self._tx, self._c._tx_conn = None, None

    async def __aenter__(self) -> "_Tx":
        await self._begin()
        return self


class PostgresConnector(DatabaseConnector):
    dialect = "postgres"

    def __init__(self, url: str, **options: Any):
        super().__init__(url, **options)
        self._pool: Any = None
        self._tx_conn: Any = None

    async def connect(self) -> None:
        try:
            import asyncpg  # type: ignore
        except ImportError as e:
            raise RuntimeError("pip install ikarem[postgres] (needs asyncpg)") from e
        self._pool = await asyncpg.create_pool(self.url, **self.options)
        self.connected = True

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
        self.connected = False

    async def execute(self, query: str, *params: Any) -> Any:
        assert self._pool, "Not connected"
        q = _pg(query)
        async with self._pool.acquire() as c:
            return await c.execute(q, *params)

    async def fetch_one(self, query: str, *params: Any) -> dict | None:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as c:
            row = await c.fetchrow(_pg(query), *params)
            return dict(row) if row else None

    async def fetch_all(self, query: str, *params: Any) -> list[dict]:
        assert self._pool, "Not connected"
        async with self._pool.acquire() as c:
            return [dict(r) for r in await c.fetch(_pg(query), *params)]

    def transaction(self) -> Transaction:
        return _Tx(self)


def _pg(query: str) -> str:
    import re

    i = 0

    def _r(_: Any) -> str:
        nonlocal i
        i += 1
        return f"${i}"

    return re.sub(r"\?", _r, query)
