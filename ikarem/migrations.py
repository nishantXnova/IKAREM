"""Version-controlled schema migrations. Zero deps, engine-agnostic.

Convention: ``migrations/NNNN_name.sql`` with up/down sections::

    -- migrate:up
    CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE);
    -- migrate:down
    DROP TABLE users;

Applied versions live in the ``ikarem_migrations`` table, so ``up`` is
idempotent and ``down`` rolls back in reverse. Statements split on
semicolons at line ends — dollar-quoted Postgres bodies are the known
limitation (keep functions in few statements or split files).

CLI: ``ikarem migrate up|down|status|new`` (see cli.py).
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

_VERSION_RE = re.compile(r"^(\d+)_.*\.sql$")
_TABLE = "ikarem_migrations"


def discover(directory: str | Path) -> list[tuple[int, Path]]:
    """Sorted (version, path) for well-named files; ignores the rest."""
    out = []
    d = Path(directory)
    if not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        m = _VERSION_RE.match(p.name)
        if m and p.is_file():
            out.append((int(m.group(1)), p))
    return sorted(out)


def split_sections(text: str) -> tuple[list[str], list[str]]:
    ups: list[str] = []
    downs: list[str] = []
    cur = ups
    for line in text.splitlines():
        s = line.strip().lower()
        if s == "-- migrate:up":
            cur = ups
            continue
        if s == "-- migrate:down":
            cur = downs
            continue
        cur.append(line)
    return _stmts("\n".join(ups)), _stmts("\n".join(downs))


def _stmts(block: str) -> list[str]:
    """Split on semicolons at line ends; strip comments/empties."""
    stmts = []
    buf: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt.strip(";").strip():
                stmts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail.strip(";").strip():
        stmts.append(tail if tail.endswith(";") else tail + ";")
    return stmts


def new_migration(directory: str | Path, name: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "migration"
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    existing = [v for v, _ in discover(d)]
    nxt = (max(existing) + 1) if existing else 1
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    path = d / f"{nxt:04d}_{slug}_{stamp}.sql"
    path.write_text(
        "-- migrate:up\n-- e.g. CREATE TABLE users (id TEXT PRIMARY KEY);\n\n-- migrate:down\n-- e.g. DROP TABLE users;\n"
    )
    return path


class Migrator:
    def __init__(self, db: Any, directory: str | Path):
        self.db = db
        self.directory = Path(directory)

    async def _ensure_table(self) -> None:
        await self.db.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
        )

    async def applied(self) -> list[int]:
        await self._ensure_table()
        rows = await self.db.fetch_all(f"SELECT version FROM {_TABLE} ORDER BY version")
        return [r["version"] for r in rows]

    async def status(self) -> dict:
        have = set(await self.applied())
        all_versions = discover(self.directory)
        return {
            "applied": sorted(have),
            "pending": [(v, p.name) for v, p in all_versions if v not in have],
            "files": [(v, p.name) for v, p in all_versions],
        }

    async def up(self, target: int | None = None) -> list[int]:
        """Apply pending migrations in order (up to target). Returns applied."""
        await self._ensure_table()
        have = set(await self.applied())
        done = []
        for v, path in discover(self.directory):
            if v in have:
                continue
            if target is not None and v > target:
                break
            ups, _downs = split_sections(path.read_text())
            for stmt in ups:
                await self.db.execute(stmt)
            await self.db.execute(
                f"INSERT INTO {_TABLE} (version, name, applied_at) VALUES (?, ?, ?)",
                v,
                path.name,
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            done.append(v)
        return done

    async def down(self, steps: int = 1) -> list[int]:
        """Roll back the last `steps` applied migrations. Returns reverted."""
        await self._ensure_table()
        have = await self.applied()
        by_version = {v: p for v, p in discover(self.directory)}
        done = []
        for v in reversed(have[-steps:] if steps else []):
            path = by_version.get(v)
            if path is not None:
                _ups, downs = split_sections(path.read_text())
                for stmt in downs:
                    await self.db.execute(stmt)
            await self.db.execute(f"DELETE FROM {_TABLE} WHERE version = ?", v)
            done.append(v)
        return done
