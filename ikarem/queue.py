"""Durable task queue: SQLite-backed jobs with retries, zero deps.

BackgroundTasks fire-and-forget in-process (gone on restart, no retry).
When a job must survive deploys — welcome emails, PDF reports, batch
imports — enqueue it instead::

    queue = QueuePlugin(app)          # tables + app.state_queue
    queue.task("welcome")(send_email)  # name -> callable registry

    # handler: await req.app.state_queue.enqueue("welcome", {"to": ...})

    # worker: ikarem worker myapp:app [--queue default] [--poll 1.0]

Lease protocol is portable (no FOR UPDATE): claim with a unique token,
confirm by reading the token back. Failed jobs back off (2^attempts sec)
and park after max_attempts for inspection instead of vanishing.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
import time
from typing import Any

_TABLE = "ikarem_queue"

_TASKS: dict[str, Any] = {}


def task(name: str):
    """Register a callable under a task name for workers to dispatch."""

    def deco(fn: Any) -> Any:
        _TASKS[name] = fn
        return fn

    return deco


def _now() -> float:
    return time.time()


class Queue:
    def __init__(self, db: Any, name: str = "default"):
        self.db = db
        self.name = name

    async def _ensure(self) -> None:
        if getattr(self.db, "dialect", "sqlite") == "postgres":
            await self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {_TABLE} (id SERIAL PRIMARY KEY,"
                " q TEXT, payload TEXT, attempts INTEGER DEFAULT 0,"
                " max_attempts INTEGER DEFAULT 5, available_at DOUBLE PRECISION,"
                " locked_by TEXT, created_at DOUBLE PRECISION)"
            )
        else:
            await self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {_TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " q TEXT, payload TEXT, attempts INTEGER DEFAULT 0,"
                " max_attempts INTEGER DEFAULT 5, available_at REAL,"
                " locked_by TEXT, created_at REAL)"
            )

    async def enqueue(
        self, task_name: str, payload: dict | None = None, delay: float = 0, max_attempts: int = 5
    ) -> Any:
        await self._ensure()
        body = json.dumps({"task": task_name, "args": payload or {}})
        return await self.db.execute(
            f"INSERT INTO {_TABLE} (q, payload, attempts, max_attempts, available_at, created_at)"
            " VALUES (?, ?, 0, ?, ?, ?)",
            self.name,
            body,
            max_attempts,
            _now() + delay,
            _now(),
        )

    async def depth(self) -> int:
        await self._ensure()
        row = await self.db.fetch_one(
            f"SELECT COUNT(*) AS n FROM {_TABLE} WHERE q = ? AND available_at IS NOT NULL", self.name
        )
        return int(row["n"]) if row else 0

    async def lease(self, timeout: float = 30) -> dict | None:
        """Claim one due job; None when the queue is empty."""
        await self._ensure()
        cands = await self.db.fetch_all(
            f"SELECT id FROM {_TABLE} WHERE q = ? AND available_at IS NOT NULL"
            " AND available_at <= ? ORDER BY id LIMIT 3",
            self.name,
            _now(),
        )
        for c in cands:
            token = secrets.token_hex(8)
            await self.db.execute(
                f"UPDATE {_TABLE} SET locked_by = ?, available_at = ?"
                " WHERE id = ? AND (locked_by IS NULL OR available_at <= ?)",
                token,
                _now() + timeout,
                c["id"],
                _now(),
            )
            row = await self.db.fetch_one(
                f"SELECT * FROM {_TABLE} WHERE id = ? AND locked_by = ?", c["id"], token
            )
            if row:
                return row
        return None

    async def complete(self, job_id: Any) -> None:
        await self.db.execute(f"DELETE FROM {_TABLE} WHERE id = ?", job_id)

    async def fail(self, job: dict, error: str = "") -> str:
        """Backoff and requeue, or park when attempts run out. Returns fate."""
        attempts = int(job.get("attempts", 0)) + 1
        if attempts >= int(job.get("max_attempts", 5)):
            await self.db.execute(
                f"UPDATE {_TABLE} SET attempts = ?, locked_by = NULL, available_at = NULL WHERE id = ?",
                attempts,
                job["id"],
            )
            return "parked"
        await self.db.execute(
            f"UPDATE {_TABLE} SET attempts = ?, locked_by = NULL, available_at = ? WHERE id = ?",
            attempts,
            _now() + 2.0**attempts,
            job["id"],
        )
        return "retry"

    async def run_one(self) -> bool:
        """Lease, dispatch to the registered task, settle. False when empty."""
        job = await self.lease()
        if job is None:
            return False
        try:
            body = json.loads(job["payload"])
        except Exception:
            await self.fail(job, "bad payload")
            return True
        fn = _TASKS.get(body.get("task", ""))
        if fn is None:
            await self.fail(job, f"unknown task {body.get('task')!r}")
            return True
        try:
            r = fn(**(body.get("args") or {}))
            if inspect.isawaitable(r):
                await r
            await self.complete(job["id"])
        except Exception as e:  # noqa: BLE001 - job failure is data, not a crash
            await self.fail(job, str(e)[:500])
        return True


class QueuePlugin:
    """DatabasePlugin-shaped wiring: tables + ``app.state_queue``."""

    name = "queue"
    priority = 20

    def __init__(self, queue_name: str = "default"):
        self.queue_name = queue_name
        self.queue: Queue | None = None

    def register(self, app: Any) -> None:
        async def _startup() -> None:
            db = getattr(app, "state_db", None)
            if db is None:
                raise RuntimeError("QueuePlugin needs DatabasePlugin registered first")
            self.queue = Queue(db, self.queue_name)
            await self.queue._ensure()
            app.state_queue = self.queue  # type: ignore

        app.on_startup(_startup)


async def run_worker(app: Any, poll: float = 1.0, stop: Any = None) -> int:
    """Drain app.state_queue until stop() is truthy. Returns jobs done."""
    queue = getattr(app, "state_queue", None)
    if queue is None:
        raise RuntimeError("no queue: register QueuePlugin (after DatabasePlugin)")
    done = 0
    while True:
        if stop is not None and stop():
            return done
        if await queue.run_one():
            done += 1
        else:
            await asyncio.sleep(poll)
