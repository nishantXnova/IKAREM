"""Ops batch: sanitization, XML, API keys/scopes/ABAC, migrations, queue, cron."""

import asyncio

import pytest

from ikarem import (
    APIKeyAuth,
    Depends,
    Ikarem,
    Migrator,
    Queue,
    QueuePlugin,
    Scheduler,
    Schema,
    ValidationError,
    create_token,
    dict_to_xml,
    escape_html,
    parse_cron,
    require_if,
    require_roles,
    require_scopes,
    task,
)
from ikarem.db import DatabasePlugin
from ikarem.http import XMLResponse
from ikarem.migrations import discover, new_migration, split_sections
from ikarem.testing import TestClient

SECRET = "ops-secret"


def test_schema_extra_forbid_and_ignore():
    class Open(Schema):
        a: int = 0

    class Strict(Schema, extra="forbid"):
        a: int = 0

    assert Open.validate({"a": 1, "zzz": 2}).dict() == {"a": 1}
    with pytest.raises(ValidationError) as e:
        Strict.validate({"a": 1, "zzz": 2})
    assert e.value.errors[0]["field"] == "zzz"
    assert Strict.validate({"a": 1}).dict() == {"a": 1}
    with pytest.raises(TypeError):

        class Bad(Schema, extra="sometimes"):
            pass


def test_xml_response_and_escaping():
    assert escape_html('<b>"x"&') == "&lt;b&gt;&quot;x&quot;&amp;"
    x = dict_to_xml({"user": {"name": "a&b", "tags": ["x", "y"], "n": 3, "ok": True, "nil": None}})
    assert "<name>a&amp;b</name>" in x and "<item>x</item>" in x
    assert "<ok>true</ok>" in x and "<nil/>" in x and x.startswith("<?xml")
    r = XMLResponse({"a": 1})
    assert r.media_type == "application/xml" and b"<a>1</a>" in r.body


def _authed_app():
    app = Ikarem(enable_docs=False, auth_secret=SECRET)
    admin = create_token("u1", SECRET, roles=["admin"], scope="read write")
    user = create_token("u2", SECRET, roles=["user"], scope="read")

    @app.get("/roles")
    async def roles(req, claims=Depends(require_roles("admin"))):
        return {"sub": claims["sub"]}

    @app.get("/scopes")
    async def scopes(req, claims=Depends(require_scopes("write"))):
        return {"sub": claims["sub"]}

    @app.get("/tenant")
    async def tenant(req, claims=Depends(require_if(lambda c: c.get("sub") == "u1", "wrong tenant"))):
        return {"ok": True}

    keys = APIKeyAuth({"svc-key": {"svc": "billing"}})

    @app.get("/svc")
    async def svc(req, info=Depends(keys)):
        return info

    return app, admin, user


def _bearer(tok):
    return {"headers": {"authorization": f"Bearer {tok}"}}


def test_scopes_roles_abac():
    app, admin, user = _authed_app()
    c = TestClient(app)
    assert c.get("/roles", **_bearer(admin)).status_code == 200
    assert c.get("/roles", **_bearer(user)).status_code == 403
    assert c.get("/scopes", **_bearer(admin)).status_code == 200
    assert c.get("/scopes", **_bearer(user)).status_code == 403
    assert c.get("/tenant", **_bearer(admin)).status_code == 200
    assert c.get("/tenant", **_bearer(user)).status_code == 403


def test_api_key_auth():
    app, _, _ = _authed_app()
    c = TestClient(app)
    assert c.get("/svc").status_code == 401
    assert c.get("/svc", headers={"x-api-key": "nope"}).status_code == 401
    assert c.get("/svc", headers={"x-api-key": "svc-key"}).json() == {"svc": "billing"}


def test_api_key_multi_key_lookup():
    # Constant-time scan across the table: every key resolves, unknowns 401.
    from ikarem import Depends, Ikarem

    app = Ikarem(enable_docs=False)
    keys = APIKeyAuth({"aaa": {"svc": "a"}, "zzz": {"svc": "z"}})

    @app.get("/svc")
    async def svc(req, info=Depends(keys)):
        return info

    c = TestClient(app)
    assert c.get("/svc", headers={"x-api-key": "aaa"}).json() == {"svc": "a"}
    assert c.get("/svc", headers={"x-api-key": "zzz"}).json() == {"svc": "z"}
    assert c.get("/svc", headers={"x-api-key": "aa"}).status_code == 401
    assert c.get("/svc", headers={"x-api-key": ""}).status_code == 401


def test_migrations_up_down_status(tmp_path):
    async def go():
        from ikarem.db import SQLiteConnector

        db = SQLiteConnector("sqlite:///:memory:")
        await db.connect()
        (tmp_path / "0001_users.sql").write_text(
            "-- migrate:up\nCREATE TABLE users (id TEXT PRIMARY KEY);\n-- migrate:down\nDROP TABLE users;\n"
        )
        (tmp_path / "0002_notes.sql").write_text(
            "-- migrate:up\nCREATE TABLE notes (id INTEGER PRIMARY KEY);\n-- migrate:down\nDROP TABLE notes;\n"
        )
        (tmp_path / "notes.txt").write_text("ignored")
        m = Migrator(db, tmp_path)
        assert [v for v, _ in (await m.status())["files"]] == [1, 2]
        assert await m.up() == [1, 2]
        assert await m.up() == []  # idempotent
        assert (await m.status())["pending"] == []
        assert await m.down() == [2]
        tables = [r["name"] for r in await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")]
        assert "notes" not in tables and "users" in tables
        assert await m.down(5) == [1]
        await db.disconnect()

    asyncio.run(go())


def test_migrations_split_and_new(tmp_path):
    ups, downs = split_sections(
        "-- migrate:up\nCREATE TABLE a (x INT);\nCREATE TABLE b (y INT);\n-- migrate:down\nDROP TABLE b;\nDROP TABLE a;\n"
    )
    assert len(ups) == 2 and len(downs) == 2
    p = new_migration(tmp_path, "Add Users!")
    assert p.name.startswith("0001_add_users_") and p.suffix == ".sql"
    assert "-- migrate:up" in p.read_text() and "-- migrate:down" in p.read_text()
    assert discover(tmp_path / "nope") == []


def test_queue_enqueue_lease_retry_park():
    async def go():
        from ikarem.db import SQLiteConnector

        db = SQLiteConnector("sqlite:///:memory:")
        await db.connect()
        q = Queue(db)
        assert await q.depth() == 0
        await q.enqueue("nosuch", {"a": 1}, max_attempts=1)
        assert await q.depth() == 1
        assert await q.run_one() is True  # unknown task -> fail -> parked (max 1)
        assert await q.depth() == 0  # parked jobs aren't due
        assert await q.run_one() is False

        done = []

        @task("add")
        def add(x=0, y=0):
            done.append(x + y)

        await q.enqueue("add", {"x": 2, "y": 3})
        assert await q.run_one() is True
        assert done == [5]
        assert await q.depth() == 0

        calls = []

        @task("flaky")
        def flaky():
            calls.append(1)
            raise RuntimeError("boom")

        await q.enqueue("flaky", max_attempts=2)
        assert await q.run_one() is True  # fail 1 -> retry (backoff, not due)
        assert await q.run_one() is False
        row = await db.fetch_one("SELECT attempts FROM ikarem_queue")
        assert row["attempts"] == 1
        await db.disconnect()

    asyncio.run(go())


def test_queue_plugin_and_worker():
    async def go():
        from ikarem.queue import run_worker

        app = Ikarem(enable_docs=False)
        app.register(DatabasePlugin("sqlite:///:memory:"))
        app.register(QueuePlugin())
        await app.startup()
        seen = []

        @task("ping")
        async def ping(n=0):
            seen.append(n)

        await app.state_queue.enqueue("ping", {"n": 7})
        done = await run_worker(app, poll=0.01, stop=lambda: bool(seen))
        assert done >= 1 and seen == [7]
        await app.shutdown()

    asyncio.run(go())


def test_cron_parse_and_match():
    assert parse_cron("@daily") == parse_cron("0 0 * * *")
    assert parse_cron("@hourly") == parse_cron("0 * * * *")
    m, h, dom, mon, dow = parse_cron("*/15 9-17 * * mon-fri")
    assert m == {0, 15, 30, 45} and h == set(range(9, 18))
    assert dow == {1, 2, 3, 4, 5}
    assert parse_cron("* * * * 7")[4] == {0}  # Sunday spelled 7
    with pytest.raises(ValueError):
        parse_cron("* * * *")
    with pytest.raises(ValueError):
        parse_cron("61 * * * *")


def test_scheduler_tick_with_fake_clock():
    async def go():
        now = [1_700_000_000.0]
        s = Scheduler(clock=lambda: now[0])
        fired = []

        @s.every(10)
        def beat():
            fired.append("beat")

        @s.cron("0 0 1 1 *")  # yearly: does not match the fake clock
        def yearly():
            fired.append("year")

        assert await s.tick() == []  # nothing due yet
        now[0] += 11
        assert await s.tick() == ["beat"]

        m = Scheduler(clock=lambda: now[0])
        mins = []

        @m.cron("* * * * *")
        async def minute():
            mins.append(1)

        assert await m.tick() == ["minute"]  # current minute matches
        assert await m.tick() == []  # same minute: no double-fire
        now[0] += 61
        assert await m.tick() == ["minute"]

        async def bad():
            raise RuntimeError("sick")

        s.every(1, name="sick")(bad)
        now[0] += 5
        await s.tick()  # sick job errors, loop survives
        assert s.jobs[-1].errors == 1
        # app integration: decorators register, explicit start runs
        app = Ikarem(enable_docs=False)
        hits = []

        @app.every(0.01)
        def hb():
            hits.append(1)

        stopped = []

        def stop():
            return bool(stopped)

        async def stopper():
            await asyncio.sleep(0.05)
            stopped.append(1)

        await asyncio.gather(app.start_scheduler(stop, poll=0.01), stopper())
        assert hits

    asyncio.run(go())
