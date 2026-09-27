"""Background tasks: run after the response is sent."""

from __future__ import annotations

import inspect
from typing import Any, Callable


class BackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list[tuple[Callable, tuple, dict]] = []

    def add(self, fn: Callable, *a: Any, **k: Any) -> None:
        self.tasks.append((fn, a, k))

    async def __call__(self) -> None:
        for fn, a, k in self.tasks:
            try:
                r = fn(*a, **k)
                if inspect.isawaitable(r):
                    await r
            except Exception:
                pass  # never fail the response because of background work

    def schedule(self, fn: Callable, *a: Any, **k: Any) -> None:
        """Fire-and-forget variant."""
        self.add(fn, *a, **k)
