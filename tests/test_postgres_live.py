"""Live Postgres strategy tests. Run only with IKAREM_TEST_PG_URL set
(locally: boot any Postgres; CI provides a service container).
Skipped otherwise — the suite must stay runnable with zero services.
"""

import asyncio
import os

import pytest

URL = os.environ.get("IKAREM_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="needs IKAREM_TEST_PG_URL")


def test_postgres_crud_placeholders_returning():
    from ikarem.db import create_connector

    async def go():
        db = create_connector(URL)
        assert db.dialect == "postgres"
        await db.connect()
        try:
            await db.execute("DROP TABLE IF EXISTS ikarem_probe")
            await db.execute("CREATE TABLE ikarem_probe (id SERIAL PRIMARY KEY, name TEXT)")
            await db.execute("INSERT INTO ikarem_probe (name) VALUES (?)", "a")
            await db.execute_many("INSERT INTO ikarem_probe (name) VALUES (?)", [("b",), ("c",)])
            assert await db.fetch_one("SELECT * FROM ikarem_probe WHERE name=?", "a") is not None
            assert len(await db.fetch_all("SELECT * FROM ikarem_probe")) == 3
            row = await db.fetch_one("INSERT INTO ikarem_probe (name) VALUES (?) RETURNING id", "z")
            assert isinstance(row["id"], int)
            assert await db.fetch_one("SELECT 1") is not None
        finally:
            try:
                await db.execute("DROP TABLE ikarem_probe")
            except Exception:
                pass
            await db.disconnect()

    asyncio.run(go())


def test_postgres_limit_offset_params():
    from ikarem.db import create_connector

    async def go():
        db = create_connector(URL)
        await db.connect()
        try:
            rows = await db.fetch_all("SELECT generate_series(1, 20) AS n LIMIT ? OFFSET ?", 5, 10)
            assert [r["n"] for r in rows] == [11, 12, 13, 14, 15]
        finally:
            await db.disconnect()

    asyncio.run(go())
