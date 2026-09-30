"""NISH responses: serve API data the NISH Viewer extension beautifies.

NISH ("the post-JSON data format" — graphs, canonical hashes, binary twin)
is a separate project with its own engines. This module is deliberately
small: a stdlib-only *writer* for the response subset (scalars, lists,
tables), so any handler can answer in NISH with no new dependency::

    from ikarem.nish import NISHResponse, negotiate

    @app.get("/api/summary")
    async def summary(req):
        data = {"income_cents": 1200, "by_category": [{"category": "food"}]}
        return negotiate(req, data)  # ?format=nish or Accept: *nish* -> NISH

Why ``text/plain`` by default: browsers *download* unknown MIME types, so
``application/x-nish`` would never reach the Viewer extension — the page
would never render. ``text/plain`` renders as text, the extension sniffs
the ``NISH/1.0`` first line and paints the tree. Pass
``media_type="application/x-nish"`` for strict machine APIs.

Limits (writer-only, on purpose): no anchors/graphs, no ext tags, no
binary twin — for the full format use the ``nish-format`` engines. Values
outside the subset raise ``TypeError`` naming the problem, never silently
degraded.
"""

from __future__ import annotations

import base64
import math
from datetime import date, datetime, timezone
from typing import Any

NISH_VERSION = "NISH/1.0"

_SAFE_KEY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _is_safe_key(key: str) -> bool:
    # Bare keys only: a dot would compose (`a.b = 1` nests), a leading
    # digit needs quoting. Anything else is double-quoted (always literal).
    return bool(key) and all(c in _SAFE_KEY for c in key) and not key[0].isdigit()


def _escape_string(value: str) -> str:
    out = ['"']
    for ch in value:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _encode_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError(
            f"NISH has no canonical form for non-finite floats ({value!r}): "
            "drop the value or encode it as a string"
        )
    if value.is_integer():
        return f"{int(value)}.0"
    return repr(value)


def _encode_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, str):
        return _escape_string(value)
    if isinstance(value, bytes):
        return "bytes:b64:" + base64.b64encode(value).decode()
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return "time:" + stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return f"time:{value.isoformat()}T00:00:00Z"
    raise TypeError(
        f"NISH writer has no encoding for {type(value).__name__}: "
        "convert it to str/int/float/bool/None/list/dict/bytes/datetime first"
    )


def _encode_key(key: Any) -> str:
    if not isinstance(key, str):
        raise TypeError(f"NISH keys must be strings, got {type(key).__name__}")
    return key if _is_safe_key(key) else _escape_string(key)


def _encode_inline(value: Any) -> str:
    """Any value as a single-line expression (inline maps/lists nest)."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_encode_key(k)} = {_encode_inline(v)}" for k, v in value.items()) + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_encode_inline(v) for v in value) + "]"
    return _encode_scalar(value)


def _emit_items(lines: list[str], prefix: str | None, mapping: dict) -> None:
    """Two passes, because section headers move the current prefix: first
    every plain key (scalars and inline values), then sub-maps as dotted
    ``[a.b]`` sections, then arrays-of-maps as ``[[k]]`` blocks last. A
    bare key after a section header would bind inside it — ordering
    prevents that entire class of misbinding."""
    if prefix is not None:
        lines.append(f"[{prefix}]")
    for key, value in mapping.items():
        if isinstance(value, dict) and _is_safe_key(key):
            continue
        if (
            isinstance(value, list)
            and value
            and all(isinstance(v, dict) for v in value)
            and _is_safe_key(key)
            and prefix is None
        ):
            continue
        lines.append(f"{_encode_key(key)} = {_encode_inline(value)}")
    for key, value in mapping.items():
        if isinstance(value, dict) and _is_safe_key(key):
            _emit_items(lines, key if prefix is None else f"{prefix}.{key}", value)
    if prefix is None:
        for key, value in mapping.items():
            if (
                isinstance(value, list)
                and value
                and all(isinstance(v, dict) for v in value)
                and _is_safe_key(key)
            ):
                for item in value:
                    lines.append(f"[[{key}]]")
                    for sk, sv in item.items():
                        lines.append(f"{_encode_key(sk)} = {_encode_inline(sv)}")


def to_nish(data: dict) -> str:
    """Serialize a JSON-shaped dict to ``NISH/1.0`` text (viewer-compatible).

    Raises ``TypeError`` for non-dict roots, non-string keys, and values
    outside the subset; ``ValueError`` for non-finite floats.
    """
    if not isinstance(data, dict):
        raise TypeError(f"NISH documents are maps, got {type(data).__name__}: wrap it in a dict")
    lines = [NISH_VERSION, ""]
    _emit_items(lines, None, data)
    return "\n".join(lines) + "\n"


def NISHResponse(
    data: dict,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    media_type: str = "text/plain; charset=utf-8",
) -> Any:
    """A ``to_nish(data)`` body browsers render (so the NISH Viewer
    extension paints it). Pass ``media_type="application/x-nish"`` for
    strict machine APIs that never open in a browser."""
    from .http import Response

    return Response(to_nish(data).encode(), status_code, headers, media_type)


def negotiate(req: Any, data: dict, status_code: int = 200) -> Any:
    """``?format=nish`` or an ``Accept`` mentioning ``nish`` answers NISH;
    everything else answers JSON. One line per route, both shapes tested."""
    from .http import JSONResponse

    if req.query.get("format") == "nish" or "nish" in req.headers.get("accept", "").lower():
        return NISHResponse(data, status_code)
    return JSONResponse(data, status_code=status_code)
