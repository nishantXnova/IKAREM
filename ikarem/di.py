"""FastAPI-style DI: Depends with per-request cache + yield cleanup.

def get_db(req): ... (sync or async, or generator with yield)
@app.get("/x")
async def h(req, db=Depends(get_db)): ...

Nested depends, use_cache, and class-based deps supported. Zero-dep.

Cleanup contract: yield-dependencies register their generators on the
request; the application runs run_cleanups() AFTER the response is sent
(and also on the exception path), mirroring FastAPI semantics.
"""

from __future__ import annotations

import inspect
from typing import Any


class Depends:
    def __init__(self, dep: Any = None, use_cache: bool = True):
        self.dep = dep
        self.use_cache = use_cache


def _coerce_query(value: str, ann: Any) -> Any:
    if ann is int:
        return int(value)
    if ann is float:
        return float(value)
    if ann is bool:
        return value.lower() in ("1", "true", "yes", "on")
    return value


def _dep_kwargs(fn: Any, request: Any) -> dict:
    """Build kwargs for a dependency callable (nested Depends supported)."""
    sig = inspect.signature(fn)
    kwargs: dict = {}
    for name, p in sig.parameters.items():
        ann = p.annotation
        if name in ("request", "req"):
            kwargs[name] = request
            continue
        if isinstance(p.default, Depends):
            # resolved by caller (_call) so it can thread cache + cycle stack
            continue
        if name in getattr(request, "path_params", {}):
            kwargs[name] = request.path_params[name]
            continue
        if name in getattr(request, "query", {}):
            kwargs[name] = _coerce_query(request.query[name], ann)
            continue
        if p.default is not inspect._empty:
            kwargs[name] = p.default
            continue
        kwargs[name] = request
    return kwargs


async def _call(dep: Any, request: Any, cache: dict | None, use_cache: bool = True, stack: tuple = ()) -> Any:
    if isinstance(dep, Depends):
        return await _call(dep.dep, request, cache, dep.use_cache, stack)
    if dep is None:
        raise ValueError("Depends() needs a callable")
    if not callable(dep):
        # primitive value dependency: Depends(42) etc.
        return dep
    name = getattr(dep, "__name__", repr(dep))
    if id(dep) in stack:
        raise ValueError(f"Circular dependency detected involving '{name}'")
    key = id(dep)
    if use_cache and cache is not None and key in cache:
        return cache[key]
    stack = stack + (key,)
    kwargs = _dep_kwargs(dep, request)
    # resolve nested Depends params now (with cache + cycle tracking)
    sig = inspect.signature(dep)
    for pname, p in sig.parameters.items():
        if isinstance(p.default, Depends):
            kwargs[pname] = await _call(p.default, request, cache, p.default.use_cache, stack)
    if inspect.isasyncgenfunction(dep):
        agen = dep(**kwargs)
        try:
            value = await agen.__anext__()
        except StopAsyncIteration:
            raise RuntimeError(f"async generator dependency '{name}' yielded nothing")
        _register_cleanup(request, agen)
        result = value
    elif inspect.isgeneratorfunction(dep):
        gen = dep(**kwargs)
        try:
            result = next(gen)
        except StopIteration:
            raise RuntimeError(f"generator dependency '{name}' yielded nothing")
        _register_cleanup(request, gen)
        result = result
    elif inspect.iscoroutinefunction(dep):
        result = await dep(**kwargs)
    else:
        result = dep(**kwargs)
        if inspect.isawaitable(result):
            result = await result
    if use_cache and cache is not None:
        cache[key] = result
    return result


def _register_cleanup(request: Any, gen: Any) -> None:
    cleanups = getattr(request, "_dep_cleanups", None)
    if cleanups is None:
        request._dep_cleanups = cleanups = []
    cleanups.append(gen)


async def run_cleanups(request: Any) -> None:
    """Finalize yield-dependencies. Safe to call twice; errors swallowed."""
    gens = getattr(request, "_dep_cleanups", [])
    request._dep_cleanups = []
    for gen in gens:
        try:
            if inspect.isasyncgen(gen):
                try:
                    await gen.__anext__()
                except StopAsyncIteration:
                    pass
                finally:
                    try:
                        await gen.aclose()
                    except Exception:
                        pass
            else:
                try:
                    next(gen)
                except StopIteration:
                    pass
                finally:
                    try:
                        gen.close()
                    except Exception:
                        pass
        except Exception:
            pass


async def resolve_handler(handler: Any, request: Any) -> Any:
    """Call handler(request, ...deps) resolving Depends + Schema body + BackgroundTasks.

    Yield-dependency finalization is NOT done here — the application runs
    run_cleanups() after the response is sent (see app._handle_http).
    Standalone callers must `await run_cleanups(request)` themselves
    (finally-safe: also call it on the exception path).

    Compiled fast path: signature parsed once per handler (see compiled.py),
    zero inspect.signature calls per request.
    """
    from .compiled import resolve_compiled

    return await resolve_compiled(handler, request)
