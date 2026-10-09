"""Hermetic backup tests: tmp SQLite files, tmp dest dirs, no shared state."""

import sqlite3

import pytest
from ikarem_backup import BackupManager, backup_sqlite, prune_backups, verify_backup


def _db(path, tables=("users",)):
    conn = sqlite3.connect(str(path))
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute(f"INSERT INTO {t} (name) VALUES ('amy')")
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


def test_full_cycle_backup_verify_prune(tmp_path):
    import time as _time

    url = _db(tmp_path / "app.db", ("users", "orders"))
    mgr = BackupManager(url, tmp_path / "backups", keep=2)
    m1 = mgr.run()
    assert m1["tables"] == ["orders", "users"] and m1["pruned"] == []
    _time.sleep(1.1)  # filenames are second-resolution by design
    mgr.run()
    _time.sleep(1.1)
    m3 = mgr.run()
    assert len(m3["pruned"]) == 1  # third run prunes the oldest, keeps 2
    assert len(list((tmp_path / "backups").glob("*.db.gz"))) == 2


def test_uncompressed_roundtrip(tmp_path):
    url = _db(tmp_path / "app.db")
    path = backup_sqlite(url, tmp_path / "b", compress=False)
    assert path.suffix == ".db"
    assert verify_backup(path)["tables"] == ["users"]


def test_corrupt_backup_fails_drill(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all")
    with pytest.raises(ValueError, match="not a SQLite file"):
        verify_backup(bad)
    with pytest.raises(FileNotFoundError):
        verify_backup(tmp_path / "missing.db")


def test_non_sqlite_refused_with_fix(tmp_path):
    with pytest.raises(ValueError, match="pg_dump"):
        backup_sqlite("postgresql://db/x", tmp_path)
    with pytest.raises(ValueError, match=":memory:"):
        backup_sqlite("sqlite:///:memory:", tmp_path)
    with pytest.raises(FileNotFoundError, match="not found"):
        backup_sqlite(f"sqlite:///{tmp_path}/nope.db", tmp_path)


def test_prune_keeps_newest(tmp_path):
    import time

    d = tmp_path / "b"
    d.mkdir()
    for i in range(4):
        (d / f"a-2024010{i}.db.gz").write_bytes(b"x")
        time.sleep(0.01)
    deleted = prune_backups(d, keep=2)
    assert deleted == ["a-20240100.db.gz", "a-20240101.db.gz"]
    with pytest.raises(ValueError, match="keep>=1"):
        prune_backups(d, keep=0)


def test_uploader_hook_called(tmp_path):
    url = _db(tmp_path / "app.db")
    seen = []
    manifest = BackupManager(url, tmp_path / "b").run(uploader=lambda p: seen.append(str(p)) or "s3://x")
    assert manifest["uploaded"] == "s3://x" and seen and seen[0].endswith(".gz")


def test_plugin_wires_guarded_route(tmp_path):
    from ikarem_backup import BackupPlugin

    from ikarem import Ikarem
    from ikarem.db import DatabasePlugin
    from ikarem.testing import TestClient

    app = Ikarem(enable_docs=False)
    app.register(DatabasePlugin(f"sqlite:///{tmp_path}/app.db"))
    app.register(BackupPlugin(dest_dir=tmp_path / "backups"))
    c = TestClient(app)
    r = c.post("/admin/backup", body={})
    assert r.status_code == 200, r.text[:200]
    assert "backup" in r.json()


def test_plugin_refuses_without_database():
    from ikarem_backup import BackupPlugin

    from ikarem import Ikarem
    from ikarem.testing import TestClient

    app = Ikarem(enable_docs=False)
    app.register(BackupPlugin())
    with pytest.raises(RuntimeError, match="DatabasePlugin"):
        TestClient(app).post("/admin/backup", body={})


def test_plugin_refuses_non_sqlite():
    from ikarem_backup import BackupPlugin

    from ikarem import Ikarem
    from ikarem.testing import TestClient

    class _FakeDb:
        name = "database"
        requires: list = []
        priority = 10

        def __init__(self, url):
            self.url = url

        def register(self, app):
            pass

    app = Ikarem(enable_docs=False)
    app.register(_FakeDb("postgresql://db/x"))
    app.register(BackupPlugin())
    with pytest.raises(ValueError, match="pg_dump"):
        TestClient(app).post("/admin/backup", body={})
