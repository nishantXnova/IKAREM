"""Flash messages: Flask's beloved one-shot notifications, session-backed.

``flash(req, "Saved")`` in a POST handler, ``get_flashed_messages(req)``
in the next template render. Survives exactly one redirect. Zero deps —
rides on ``SessionMiddleware`` (which must be installed).
"""

from __future__ import annotations

from typing import Any

_KEY = "_flashes"


def flash(req: Any, message: str, category: str = "info") -> None:
    sess = getattr(req, "session", None)
    if sess is None:
        raise RuntimeError("flash() requires SessionMiddleware")
    # Reassign (don't mutate in place): Session only persists on setitem.
    items = list(sess.get(_KEY) or [])
    items.append({"category": category, "message": str(message)})
    sess[_KEY] = items


def get_flashed_messages(req: Any, with_categories: bool = False) -> list:
    """Read + consume queued flashes. [] when no session (template-safe)."""
    sess = getattr(req, "session", None)
    if sess is None:
        return []
    items = sess.pop(_KEY, []) or []
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append((it.get("category", "info"), it.get("message", "")))
        else:
            out.append(("info", str(it)))
    return out if with_categories else [m for _, m in out]
