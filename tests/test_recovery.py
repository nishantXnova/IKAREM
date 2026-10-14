"""Failure recovery: dead workers, competing workers. No flakes by design."""

import asyncio
import time

from ikarem.db import SQLiteConnector
from ikarem.queue import Queue, task


def _db():
    return SQLiteConnector("sqlite:///:memory:")


def test_dead_worker_lease_expires_and_recovers():
    """A worker that dies mid-job (no complete/fail) must not take the job
    to the grave: after the lease timeout another worker reaps it."""

    async def go():
        db = _db()
        await db.connect()
        qa, qb = Queue(db), Queue(db)
        await qa.enqueue("mule", {"n": 1})
        job = await qa.lease(timeout=0.05)
        assert job is not None  # worker A holds it
        assert await qb.lease(timeout=30) is None  # still locked: not due
        time.sleep(0.08)  # worker A "dies" here (never settles the job)
        again = await qb.lease(timeout=30)
        assert again is not None and again["id"] == job["id"]  # worker B recovers it
        await qb.complete(again["id"])
        assert await qb.lease() is None
        await db.disconnect()

    asyncio.run(go())


def test_two_workers_split_work_without_duplicates():
    """Portable leases (claim + read-back, no FOR UPDATE) must survive two
    workers racing the same table: every job runs exactly once."""

    async def go():
        db = _db()
        await db.connect()
        qa, qb = Queue(db), Queue(db)
        ran = []

        @task("each-once")
        def each(n=0):
            ran.append(n)

        for i in range(4):
            await qa.enqueue("each-once", {"n": i})
        while await qa.run_one() or await qb.run_one():
            pass
        assert sorted(ran) == [0, 1, 2, 3]
        await db.disconnect()

    asyncio.run(go())
