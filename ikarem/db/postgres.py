"""PostgreSQL strategy — lazy asyncpg import, core stays light."""

from __future__ import annotations

from typing import Any

from .base import DatabaseConnector, Transaction


class _Tx(Transaction):
    def __init__(self, conn: "PostgresConnector"):
        self._c = conn
        self._tx: Any = None

    async def _begin(self) -> None:
        pool = await self._c._pool_for()
        c = await pool.acquire()
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
        self._pool_loop: Any = None

    async def _pool_for(self) -> Any:
        """Pool bound to the CURRENT event loop, recreated on loop change.

        asyncpg pools are loop-bound, but callers legitimately span loops
        (TestClient runs each request on a fresh loop; apps restart loops).
        A stale pool raises InterfaceError chaos — detect and rebuild.
        """
        import asyncio

        import asyncpg  # type: ignore

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._pool is None or (
            loop is not None and self._pool_loop is not None and self._pool_loop is not loop
        ):
            if self._pool is not None:
                # terminate() is synchronous: safe from any loop, unlike
                # close() which must run on the pool's own (now dead) loop.
                try:
                    self._pool.terminate()
                except Exception:
                    pass
                self._pool = None
            options = {"min_size": 1, **self.options}
            self._pool = await asyncpg.create_pool(self.url, **options)
            self._pool_loop = loop
            self.connected = True
        return self._pool

    # NOTE: min_size=1 (not asyncpg's eager 10): pools scale to max_size on
    # demand, so an abandoned pool (dead event loop — see _pool_for) strands
    # at most one connection instead of ten. Production servers keep one
    # loop forever, so warm pools are unaffected; tune max_size via options.

    async def connect(self) -> None:
        try:
            import asyncpg  # type: ignore  # noqa: F401
        except ImportError as e:
            raise RuntimeError("pip install ikarem[postgres] (needs asyncpg)") from e
        await self._pool_for()

    async def disconnect(self) -> None:
        if self._pool:
            try:
                await self._pool.close()
            except Exception:
                pass
            self._pool = None
        self.connected = False

    async def execute(self, query: str, *params: Any) -> Any:
        pool = await self._pool_for()
        q = _pg(query)
        async with pool.acquire() as c:
            return await c.execute(q, *params)

    async def fetch_one(self, query: str, *params: Any) -> dict | None:
        pool = await self._pool_for()
        async with pool.acquire() as c:
            row = await c.fetchrow(_pg(query), *params)
            return dict(row) if row else None

    async def fetch_all(self, query: str, *params: Any) -> list[dict]:
        pool = await self._pool_for()
        async with pool.acquire() as c:
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
