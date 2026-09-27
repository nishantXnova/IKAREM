"""MySQL strategy — lazy aiomysql import."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .base import DatabaseConnector, Transaction


class _Tx(Transaction):
    def __init__(self, conn: "MySQLConnector"):
        self._c = conn

    async def commit(self) -> None:
        if self._c._conn:
            await self._c._conn.commit()

    async def rollback(self) -> None:
        if self._c._conn:
            await self._c._conn.rollback()


class MySQLConnector(DatabaseConnector):
    dialect = "mysql"

    def __init__(self, url: str, **options: Any):
        super().__init__(url, **options)
        self._pool: Any = None
        self._conn: Any = None

    def _kwargs(self) -> dict:
        u = urlparse(self.url)
        return {
            "host": u.hostname or "localhost",
            "port": u.port or 3306,
            "user": u.username or "root",
            "password": u.password or "",
            "db": (u.path or "/test").lstrip("/"),
            **self.options,
        }

    async def connect(self) -> None:
        try:
            import aiomysql  # type: ignore
        except ImportError as e:
            raise RuntimeError("pip install ikarem[mysql] (needs aiomysql)") from e
        self._pool = await aiomysql.create_pool(**self._kwargs())
        self.connected = True

    async def disconnect(self) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
        self.connected = False

    async def _q(self, query: str, params: tuple, fetch: str) -> Any:
        import aiomysql  # type: ignore

        assert self._pool, "Not connected"
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query.replace("?", "%s"), params)
                if fetch == "one":
                    return await cur.fetchone()
                if fetch == "all":
                    return await cur.fetchall()
                await conn.commit()
                return cur.lastrowid

    async def execute(self, query: str, *params: Any) -> Any:
        return await self._q(query, params, "none")

    async def fetch_one(self, query: str, *params: Any) -> dict | None:
        return await self._q(query, params, "one")

    async def fetch_all(self, query: str, *params: Any) -> list[dict]:
        return list(await self._q(query, params, "all") or [])

    def transaction(self) -> Transaction:
        return _Tx(self)
