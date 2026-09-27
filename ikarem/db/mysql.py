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
        self._pool_loop: Any = None

    async def _pool_for(self) -> Any:
        """Loop-tracked pool (see PostgresConnector._pool_for for why)."""
        import asyncio

        import aiomysql  # type: ignore

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._pool is None or (
            loop is not None and self._pool_loop is not None and self._pool_loop is not loop
        ):
            if self._pool is not None:
                # close() alone is synchronous and safe from any loop;
                # wait_closed() would need the pool's (now dead) loop.
                try:
                    self._pool.close()
                except Exception:
                    pass
                self._pool = None
            options = {"minsize": 1, **self.options}
            self._pool = await aiomysql.create_pool(**{**self._kwargs(), **options})
            self._pool_loop = loop
            self.connected = True
        return self._pool

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
            import aiomysql  # type: ignore  # noqa: F401
        except ImportError as e:
            raise RuntimeError("pip install ikarem[mysql] (needs aiomysql)") from e
        await self._pool_for()

    async def disconnect(self) -> None:
        if self._pool:
            try:
                self._pool.close()
                await self._pool.wait_closed()
            except Exception:
                pass
            self._pool = None
        self.connected = False

    async def _q(self, query: str, params: tuple, fetch: str) -> Any:
        import aiomysql  # type: ignore

        pool = await self._pool_for()
        async with pool.acquire() as conn:
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
