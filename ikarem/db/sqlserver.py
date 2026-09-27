"""SQL Server strategy — lazy aioodbc import."""

from __future__ import annotations

from typing import Any

from .base import DatabaseConnector, Transaction


class _Tx(Transaction):
    def __init__(self, conn: "SQLServerConnector"):
        self._c = conn

    async def commit(self) -> None:
        if self._c._conn:
            await self._c._conn.commit()

    async def rollback(self) -> None:
        if self._c._conn:
            await self._c._conn.rollback()


class SQLServerConnector(DatabaseConnector):
    dialect = "sqlserver"

    def __init__(self, url: str, **options: Any):
        super().__init__(url, **options)
        self._conn: Any = None

    async def connect(self) -> None:
        try:
            import aioodbc  # type: ignore
        except ImportError as e:
            raise RuntimeError("pip install ikarem[sqlserver] (needs aioodbc + ODBC driver)") from e
        self._conn = await aioodbc.connect(dsn=self.url, **self.options)
        self.connected = True

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        self.connected = False

    async def _run(self, query: str, params: tuple, fetch: str) -> Any:
        assert self._conn, "Not connected"
        async with await self._conn.cursor() as cur:
            await cur.execute(query, params)
            if fetch == "one":
                row = await cur.fetchone()
                if not row:
                    return None
                cols = [c[0] for c in cur.description]
                return dict(zip(cols, row))
            if fetch == "all":
                rows = await cur.fetchall()
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            await self._conn.commit()
            return cur.rowcount

    async def execute(self, query: str, *params: Any) -> Any:
        return await self._run(query, params, "none")

    async def fetch_one(self, query: str, *params: Any) -> dict | None:
        return await self._run(query, params, "one")

    async def fetch_all(self, query: str, *params: Any) -> list[dict]:
        return await self._run(query, params, "all")

    def transaction(self) -> Transaction:
        return _Tx(self)
