"""Upgrade promises, kept mechanically: deprecation warnings with a path forward."""

from __future__ import annotations

import functools
import warnings
from typing import Any, Callable


def deprecated(reason: str, *, since: str = "", removal: str = "", use_instead: str = "") -> Callable:
    """Mark a function/method deprecated. Emits DeprecationWarning (once per
    location by default) naming the version, the removal target, and the
    replacement — so upgrades never surprise.

    @deprecated("use new_fetch() instead", since="1.1.0", removal="2.0.0",
                use_instead="new_fetch")
    """

    def deco(fn: Callable) -> Callable:
        msg = f"{fn.__qualname__} is deprecated"
        if since:
            msg += f" since {since}"
        if removal:
            msg += f" and will be removed in {removal}"
        msg += f": {reason}" if reason else "."
        if use_instead:
            msg += f" Use {use_instead} instead."

        @functools.wraps(fn)
        def _w(*a: Any, **k: Any) -> Any:
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
            return fn(*a, **k)

        return _w

    return deco
