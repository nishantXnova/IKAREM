"""Cron scheduling: stdlib only, minute granularity, testable clock.

    app.cron("*/5 * * * *")(nightly_cleanup)   # min hour dom month dow
    app.every(30)(heartbeat)                   # every N seconds

Jobs run inside your process on the app lifecycle (explicit start —
nothing runs unless you start it)::

    stop = lambda: False
    await app.start_scheduler(stop)   # usually beside app startup

Design for testability: all timing flows through ``tick(now)``, so tests
drive time instead of sleeping. Overlapping runs of the same job never
stack: a job still running is skipped that tick.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Callable

_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip().lower()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            span = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-", 1)
            span = range(_num(a, lo, hi), _num(b, lo, hi) + 1)
        else:
            span = range(_num(part, lo, hi), _num(part, lo, hi) + 1)
        out.update(range(span.start, span.stop, step))
    return {v for v in out if lo <= v <= hi}


def _num(tok: str, lo: int, hi: int) -> int:
    tok = tok.strip().lower()
    if tok in _NAMES:
        return _NAMES[tok]
    v = int(tok)
    if tok == "7" and lo == 0 and hi == 6:  # Sunday, both spellings
        return 0
    return v


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse crontab expression -> (minute, hour, dom, month, dow) sets."""
    expr = _ALIASES.get(expr.strip().lower(), expr)
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(parts)}: {expr!r}")
    minute = _field(parts[0], 0, 59)
    hour = _field(parts[1], 0, 23)
    dom = _field(parts[2], 1, 31)
    month = _field(parts[3], 1, 12)
    dow = _field(parts[4], 0, 6)
    if not (minute and hour and dom and month and dow):
        raise ValueError(f"empty field in cron: {expr!r}")
    return minute, hour, dom, month, dow


def _matches(spec: tuple, now: Any) -> bool:
    minute, hour, dom, month, dow = spec
    # time.struct_time: tm_min tm_hour tm_mday tm_mon; tm_wday Mon=0 -> cron Sun=0
    return (
        now.tm_min in minute
        and now.tm_hour in hour
        and now.tm_mday in dom
        and now.tm_mon in month
        and (now.tm_wday + 1) % 7 in dow
    )


class Job:
    def __init__(self, name: str, fn: Callable, kind: str, spec: Any, interval: float = 0):
        self.name = name
        self.fn = fn
        self.kind = kind  # "cron" | "every"
        self.spec = spec
        self.interval = interval
        self.last_run: float = 0
        self.last_fired_minute: int = -1
        self.running = False
        self.runs = 0
        self.errors = 0


class Scheduler:
    def __init__(self, clock: Callable[[], float] | None = None):
        self.jobs: list[Job] = []
        self._clock = clock or time.time

    def cron(self, expr: str, name: str | None = None):
        spec = parse_cron(expr)

        def deco(fn: Callable) -> Callable:
            self.jobs.append(Job(name or getattr(fn, "__name__", "job"), fn, "cron", spec))
            return fn

        return deco

    def every(self, seconds: float, name: str | None = None):
        if seconds <= 0:
            raise ValueError("interval must be positive")

        def deco(fn: Callable) -> Callable:
            job = Job(name or getattr(fn, "__name__", "job"), fn, "every", None, float(seconds))
            job.last_run = self._clock()
            self.jobs.append(job)
            return fn

        return deco

    async def tick(self, now: float | None = None) -> list[str]:
        """Fire due jobs once. Returns names started. Never stacks overlaps."""
        now = self._clock() if now is None else now
        fired = []
        for job in self.jobs:
            if job.running:
                continue
            due = False
            if job.kind == "every":
                due = (now - job.last_run) >= job.interval
            else:
                minute_key = int(now // 60)
                if minute_key != job.last_fired_minute and _matches(job.spec, time.localtime(now)):
                    job.last_fired_minute = minute_key
                    due = True
            if due:
                job.running = True
                job.last_run = now
                fired.append(job.name)
                try:
                    r = job.fn()
                    if inspect.isawaitable(r):
                        await r
                    job.runs += 1
                except Exception as e:  # noqa: BLE001
                    # Contained, never silent: counted AND logged with trace.
                    from .observability import logger

                    job.errors += 1
                    logger.exception(f"scheduler job '{job.name}' failed: {e}")
                finally:
                    job.running = False
        return fired


async def run_scheduler(
    scheduler: Scheduler, stop: Callable[[], bool] | None = None, poll: float = 1.0
) -> None:
    while True:
        if stop is not None and stop():
            return
        await scheduler.tick()
        await asyncio.sleep(poll)
