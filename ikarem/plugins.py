"""Plugin architecture: protocol + dependency-sorted manager.

A plugin can:
  - declare name / requires / priority
  - register routes, middleware, config defaults, exception handlers
  - hook on_startup / on_shutdown / on_request / on_response

Manager topologically sorts by `requires` (missing dep -> error) and
by priority within levels. This is the extension point the DB plugin
(and all future extensions) build on — core never changes.
"""

from __future__ import annotations

from typing import Any, Protocol


class Plugin(Protocol):
    name: str
    requires: list[str]
    priority: int

    def register(self, app: Any) -> None: ...
    async def on_startup(self, app: Any) -> None: ...
    async def on_shutdown(self, app: Any) -> None: ...
    async def on_request(self, request: Any) -> None: ...
    async def on_response(self, request: Any, response: Any) -> None: ...


class BasePlugin:
    name = "base"
    requires: list[str] = []
    priority = 100

    def register(self, app: Any) -> None:
        pass

    async def on_startup(self, app: Any) -> None:
        pass

    async def on_shutdown(self, app: Any) -> None:
        pass

    async def on_request(self, request: Any) -> None:
        pass

    async def on_response(self, request: Any, response: Any) -> None:
        pass


class PluginManager:
    def __init__(self) -> None:
        self._plugins: dict[str, Any] = {}
        self._ordered: list[Any] = []

    def add(self, plugin: Any) -> None:
        name = getattr(plugin, "name", plugin.__class__.__name__)
        if name in self._plugins:
            raise ValueError(f"Plugin '{name}' already registered")
        self._plugins[name] = plugin
        self._ordered = self._sort()

    def _sort(self) -> list[Any]:
        # Kahn's topological sort over `requires`, tie-broken by priority.
        nodes = dict(self._plugins)
        indeg: dict[str, int] = {k: 0 for k in nodes}
        edges: dict[str, list[str]] = {k: [] for k in nodes}
        for name, p in nodes.items():
            for dep in getattr(p, "requires", []):
                if dep not in nodes:
                    raise ValueError(f"Plugin '{name}' requires missing plugin '{dep}'")
                edges[dep].append(name)
                indeg[name] += 1
        queue = sorted(
            [n for n, d in indeg.items() if d == 0], key=lambda n: getattr(nodes[n], "priority", 100)
        )
        out: list[Any] = []
        while queue:
            n = queue.pop(0)
            out.append(nodes[n])
            for m in edges[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
            queue.sort(key=lambda n: getattr(nodes[n], "priority", 100))
        if len(out) != len(nodes):
            raise ValueError("Circular plugin dependency detected")
        return out

    @property
    def plugins(self) -> list[Any]:
        return list(self._ordered)

    def get(self, name: str) -> Any | None:
        return self._plugins.get(name)

    async def startup(self, app: Any) -> None:
        for p in self._ordered:
            fn = getattr(p, "on_startup", None)
            if fn:
                await fn(app) if _is_coro(fn) else fn(app)

    async def shutdown(self, app: Any) -> None:
        for p in reversed(self._ordered):
            fn = getattr(p, "on_shutdown", None)
            if fn:
                await fn(app) if _is_coro(fn) else fn(app)


def _is_coro(fn: Any) -> bool:
    import inspect

    return inspect.iscoroutinefunction(getattr(fn, "__call__", fn)) or inspect.iscoroutinefunction(fn)
