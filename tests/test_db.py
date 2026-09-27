import asyncio

import pytest

from ikarem import Ikarem
from ikarem.db import SQLiteConnector, create_connector
from ikarem.db.plugin import DatabasePlugin


def test_factory_routing():
    assert isinstance(create_connector("sqlite:///:memory:"), SQLiteConnector)
    assert create_connector("postgresql://x").dialect == "postgres"
    assert create_connector("mysql://x").dialect == "mysql"
    with pytest.raises(ValueError):
        create_connector("oracle://x")


def test_sqlite_crud():
    async def _go():
        db = SQLiteConnector("sqlite:///:memory:")
        await db.connect()
        await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        await db.execute("INSERT INTO t (name) VALUES (?)", "a")
        await db.execute_many("INSERT INTO t (name) VALUES (?)", [("b",), ("c",)])
        assert await db.fetch_one("SELECT * FROM t WHERE name=?", "a") is not None
        assert len(await db.fetch_all("SELECT * FROM t")) == 3
        await db.disconnect()

    asyncio.run(_go())


def test_db_plugin_lifecycle():
    async def _go():
        app = Ikarem()

        @app.get("/count")
        async def count(req):
            rows = await req.app.state_db.fetch_all("SELECT * FROM t")
            return {"n": len(rows)}

        app.register(DatabasePlugin("sqlite:///:memory:"))
        await app.startup()
        await app.state_db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        await app.state_db.execute("INSERT INTO t DEFAULT VALUES")
        rows = await app.state_db.fetch_all("SELECT * FROM t")
        assert len(rows) == 1
        await app.shutdown()

    asyncio.run(_go())
