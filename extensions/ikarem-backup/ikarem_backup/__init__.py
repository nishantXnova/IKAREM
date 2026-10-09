"""SQLite backups: dump, verify (restore drill), prune, upload hook.

Every company needs backups; the core has no I/O opinions, so they live
here. Stdlib only (`sqlite3` online-backup API — safe on a live DB,
unlike file copy). Non-SQLite engines are refused with the fix, not a
traceback. Upload is a caller-provided callable so S3/GCS stay lazy
optionals with zero required dependencies.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ikarem_backup")

__all__ = ["BackupManager", "BackupPlugin", "backup_sqlite", "verify_backup", "prune_backups"]


def _source_path(db_url: str) -> Path:
    if not db_url.startswith("sqlite:///"):
        raise ValueError(
            f"ikarem-backup handles SQLite files only (got {db_url!r}): "
            "use your engine's native dump (pg_dump, mysqldump) for other backends"
        )
    path = db_url.removeprefix("sqlite:///")
    if path == ":memory:":
        raise ValueError("cannot back up :memory: databases: point DatabasePlugin at a file first")
    return Path(path)


def backup_sqlite(db_url: str, dest_dir: str | Path, compress: bool = True) -> Path:
    """Online backup of a live SQLite file. Returns the backup path."""
    src = _source_path(db_url)
    if not src.exists():
        raise FileNotFoundError(f"database file not found: {src} (is the app started against this path?)")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{src.stem}-{stamp}.db"
    s_conn = sqlite3.connect(str(src))
    d_conn = sqlite3.connect(str(dest))
    try:
        s_conn.backup(d_conn)
    finally:
        # connect() context managers only commit — close explicitly or
        # Windows locks the files (unlink/compress would fail).
        d_conn.close()
        s_conn.close()
    log.info("backup wrote %s", dest)
    if compress:
        gz = dest.with_suffix(dest.suffix + ".gz")
        with open(dest, "rb") as f_in, gzip.open(gz, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        dest.unlink()
        dest = gz
    return dest


def verify_backup(path: str | Path) -> dict:
    """Restore drill without touching prod: open the backup read-only,
    run integrity_check, list tables. Raises with the fix on failure."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"backup not found: {path}")
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rb") as f:
            head = f.read(16)
    except OSError as e:
        raise ValueError(f"backup {path.name} unreadable ({e}): re-run the backup") from e
    if head[:6] != b"SQLite" and path.suffix != ".gz":
        raise ValueError(f"backup {path.name} is not a SQLite file: re-run the backup, then verify again")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ikarem-restore-") as tmp:
        probe = Path(tmp) / "probe.db"
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as f_in, open(probe, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        else:
            shutil.copy(path, probe)
        try:
            conn = sqlite3.connect(f"file:{probe}?mode=ro", uri=True)
        except sqlite3.Error as e:
            raise ValueError(f"backup {path.name} failed to open ({e}): re-run the backup") from e
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        except sqlite3.Error as e:
            raise ValueError(f"backup {path.name} failed drill ({e}): re-run the backup") from e
        finally:
            conn.close()  # Windows locks open DB files: cleanup would fail without this
    if integrity != "ok":
        raise ValueError(f"backup {path.name} integrity_check={integrity!r}: re-run the backup")
    return {"path": str(path), "integrity": integrity, "tables": sorted(tables)}


def prune_backups(dest_dir: str | Path, keep: int = 7) -> list[str]:
    """Delete oldest backups past `keep`. Returns deleted names."""
    if keep < 1:
        raise ValueError(f"keep={keep} would delete everything: pass keep>=1")
    files = sorted(
        [p for p in Path(dest_dir).glob("*.db*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    doomed = files[: max(0, len(files) - keep)]
    for p in doomed:
        p.unlink()
    if doomed:
        log.info("backup pruned %d file(s), kept %d", len(doomed), keep)
    return [p.name for p in doomed]


class BackupManager:
    """One object for the whole drill: backup + verify + prune."""

    def __init__(self, db_url: str, dest_dir: str | Path, keep: int = 7, compress: bool = True):
        self.db_url = db_url
        self.dest_dir = Path(dest_dir)
        self.keep = keep
        self.compress = compress

    def run(self, uploader: Callable[[Path], Any] | None = None) -> dict:
        """Full cycle: dump, drill, prune, optionally upload. Returns manifest."""
        path = backup_sqlite(self.db_url, self.dest_dir, self.compress)
        report = verify_backup(path)
        deleted = prune_backups(self.dest_dir, self.keep)
        uploaded = None
        if uploader is not None:
            uploaded = uploader(path)
        manifest = {"backup": str(path), "tables": report["tables"], "pruned": deleted, "uploaded": uploaded}
        log.info("backup cycle done: %s", manifest["backup"])
        return manifest


class BackupPlugin:
    """Nightly-drill wiring: `POST /admin/backup` runs the full cycle.

    Guard it in your app (admin role / API key) — the route is yours to
    protect, like every mutating route. Pair with SchedulerPlugin or a
    cron `@app.every(86400)` calling `manager.run()` for automation.
    """

    name = "backup"
    requires: list[str] = []
    priority = 100

    def __init__(self, dest_dir: str | Path = "backups", keep: int = 7):
        self.manager: BackupManager | None = None
        self.dest_dir = dest_dir
        self.keep = keep

    def register(self, app: Any) -> None:
        self.manager = None

        async def _startup() -> None:
            db_plugin = app.plugins.get("database")
            if db_plugin is None or not getattr(db_plugin, "url", ""):
                raise RuntimeError(
                    "BackupPlugin needs DatabasePlugin registered first "
                    "(app.register(DatabasePlugin('sqlite:///app.db')) before app.register(BackupPlugin(...)))"
                )
            url = db_plugin.url
            if not url.startswith("sqlite:///"):
                raise ValueError(
                    f"BackupPlugin handles SQLite files only (got {url!r}): "
                    "use your engine's native dump (pg_dump, mysqldump) for other backends"
                )
            self.manager = BackupManager(url, self.dest_dir, self.keep)

        app.on_startup(_startup)

        @app.post("/admin/backup")
        async def run_backup(req: Any) -> dict:
            if self.manager is None:
                raise RuntimeError("BackupPlugin not ready: startup never ran (serve the app first)")
            return self.manager.run()
