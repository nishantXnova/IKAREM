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

Limits (response subset, on purpose): the writer emits no anchors/graphs,
no ext tags, no binary twin — for the full format use the ``nish-format``
engines. Values outside the subset raise ``TypeError`` naming the
problem, never silently degraded. The reader below parses general
``NISH/1.0`` core documents (sections, arrays, dotted keys, comments,
ext tags preserved as ``{"$tag", "value"}``); anchors are resolved like
the reference engine for documents that use them.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
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
    etag: bool = True,
) -> Any:
    """A ``to_nish(data)`` body browsers render (so the NISH Viewer
    extension paints it). Pass ``media_type="application/x-nish"`` for
    strict machine APIs that never open in a browser.

    Content-hash ``ETag`` on by default (disable with ``etag=False``):
    pair with ``ConditionalMiddleware`` and repolls answer 304 with no
    body when nothing changed.
    """
    from .http import Response

    body = to_nish(data).encode()
    merged = dict(headers or {})
    if etag and status_code == 200:
        merged.setdefault("etag", f'"{hashlib.sha256(body).hexdigest()}"')
    return Response(body, status_code, merged, media_type)


def negotiate(req: Any, data: dict, status_code: int = 200) -> Any:
    """``?format=nish`` or an ``Accept`` mentioning ``nish`` answers NISH;
    everything else answers JSON. One line per route, both shapes tested."""
    from .http import JSONResponse

    if req.query.get("format") == "nish" or "nish" in req.headers.get("accept", "").lower():
        return NISHResponse(data, status_code)
    return JSONResponse(data, status_code=status_code)


_BARE_KEY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}


class _Reader:
    """Char-level NISH/1.0 core parser. Superset in two harmless ways:
    comments are allowed inside brackets, and a missing ``NISH/x.y``
    header is accepted (the writer always emits it)."""

    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.line = 1
        self.anchors: dict[str, Any] = {}

    def err(self, msg: str) -> ValueError:
        return ValueError(f"NISH line {self.line}: {msg}")

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def next(self) -> str:
        c = self.peek()
        self.i += 1
        if c == "\n":
            self.line += 1
        return c

    def eof(self) -> bool:
        return self.i >= len(self.s)

    def skip_trivia(self) -> None:
        while not self.eof():
            c = self.peek()
            if c in " \t\r\n":
                self.next()
            elif c == "#":
                while not self.eof() and self.peek() != "\n":
                    self.next()
            else:
                return

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise self.err(f"expected {ch!r}, found {self.peek()!r}")
        self.next()


def _read_quoted(r: _Reader) -> str:
    r.expect('"')
    out = []
    while True:
        if r.eof():
            raise r.err("unterminated string")
        c = r.next()
        if c == '"':
            return "".join(out)
        if c == "\n":
            raise r.err("unterminated string")
        if c != "\\":
            out.append(c)
            continue
        e = r.next()
        if e in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[e])
        elif e == "u" or e == "U":
            digits = 4 if e == "u" else 8
            hexed = r.s[r.i : r.i + digits]
            if len(hexed) != digits or any(h not in "0123456789abcdefABCDEF" for h in hexed):
                raise r.err(f"bad \\{e} escape")
            out.append(chr(int(hexed, 16)))
            r.i += digits
        elif e == "":
            raise r.err("unterminated string")
        else:
            raise r.err(f"unknown escape \\{e}")


def _read_raw(r: _Reader) -> str:
    r.expect("'")
    out = []
    while True:
        if r.eof():
            raise r.err("unterminated raw string")
        c = r.next()
        if c == "'":
            if r.peek() == "'":
                r.next()
                out.append("'")
            else:
                return "".join(out)
        else:
            out.append(c)


def _read_multiline(r: _Reader) -> str:
    for _ in range(3):
        r.expect('"')
    if r.peek() == "\n":  # leading newline after opening quotes is stripped
        r.next()
    out = []
    while True:
        if r.eof():
            raise r.err("unterminated multiline string")
        if r.s.startswith('"""', r.i):
            r.i += 3
            return "".join(out)
        c = r.next()
        if c != "\\":
            out.append(c)
            continue
        e = r.next()
        if e in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[e])
        elif e == "u" or e == "U":
            digits = 4 if e == "u" else 8
            hexed = r.s[r.i : r.i + digits]
            if len(hexed) != digits or any(h not in "0123456789abcdefABCDEF" for h in hexed):
                raise r.err(f"bad \\{e} escape")
            out.append(chr(int(hexed, 16)))
            r.i += digits
        elif e == "":
            raise r.err("unterminated multiline string")
        else:
            raise r.err(f"unknown escape \\{e}")


def _read_bare(r: _Reader, extra_stop: str = "") -> str:
    start = r.i
    while not r.eof() and r.peek() not in " \t\r\n,#]}" + extra_stop:
        r.next()
    if r.i == start:
        raise r.err("expected a value")
    return r.s[start : r.i]


def _classify_bare(r: _Reader, tok: str) -> Any:
    if tok == "null":
        return None
    if tok == "true":
        return True
    if tok == "false":
        return False
    low = tok.lower()
    if low.startswith("bytes:b64:"):
        # Lenient like the reference engine: non-alphabet chars are
        # ignored (garbage decodes short, never raises).
        return base64.b64decode(tok[len("bytes:b64:") :])
    if low.startswith("bytes:hex:"):
        try:
            return bytes.fromhex(tok[len("bytes:hex:") :])
        except ValueError as e:
            raise r.err(f"bad hex payload: {e}") from e
    if low.startswith("time:"):
        from datetime import datetime

        raw = tok[len("time:") :]
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw  # spec: preserve unparseable timestamps as strings
    if re.fullmatch(r"[+-]?\d[\d_]*", tok):
        return int(tok)
    if re.fullmatch(r"[+-]?0[xX][\da-fA-F_]+|[+-]?0[oO][0-7_]+|[+-]?0[bB][01_]+", tok):
        return int(tok, 0)
    try:
        v = float(tok)
    except ValueError:
        # Bare words are strings (matches the reference engine). Quoting
        # is still recommended: a typo like `ture` parses as "ture".
        return tok
    # Non-finite floats have no NISH form — like the engine, keep the word.
    return v if math.isfinite(v) else tok


def _read_ext(r: _Reader) -> Any:
    r.expect("!")
    start = r.i
    while not r.eof() and (r.peek().isalnum() or r.peek() in "_.-"):
        r.next()
    tag = r.s[start : r.i]
    if not tag:
        raise r.err("expected a tag after '!'")
    r.skip_trivia()
    if r.peek() == "(":
        r.next()
        value = _read_value(r)
        r.skip_trivia()
        r.expect(")")
    else:
        value = _read_value(r)
    return {"$tag": tag, "value": value}


def _read_value(r: _Reader) -> Any:
    r.skip_trivia()
    c = r.peek()
    if c == '"':
        if r.s.startswith('"""', r.i):
            return _read_multiline(r)
        return _read_quoted(r)
    if c == "'":
        return _read_raw(r)
    if c == "[":
        r.next()
        items = []
        while True:
            r.skip_trivia()
            if r.peek() == "]":
                r.next()
                return items
            if r.eof():
                raise r.err("unterminated list")
            items.append(_read_value(r))
            r.skip_trivia()
            if r.peek() == ",":
                r.next()
            elif r.peek() == "]":
                continue
            else:
                raise r.err(f"expected ',' or ']' in list, found {r.peek()!r}")
    if c == "{":
        r.next()
        mapping: dict[str, Any] = {}
        while True:
            r.skip_trivia()
            if r.peek() == "}":
                r.next()
                return mapping
            if r.eof():
                raise r.err("unterminated map")
            key, quoted = _read_key(r)
            r.skip_trivia()
            if r.peek() == "=" or r.peek() == ":":
                r.next()
            else:
                raise r.err(f"expected '=' or ':' in map, found {r.peek()!r}")
            _assign(mapping, key, quoted, _read_value(r), r)
            r.skip_trivia()
            if r.peek() == ",":
                r.next()
            elif r.peek() == "}":
                continue
            else:
                raise r.err(f"expected ',' or '}}' in map, found {r.peek()!r}")
    if c == "!":
        return _read_ext(r)
    if c == "&":
        import copy

        r.next()
        name = _read_bare(r)
        r.skip_trivia()
        value = _read_value(r)
        r.anchors[name] = value
        return copy.deepcopy(value)
    if c == "*":
        import copy

        r.next()
        name = _read_bare(r)
        if name not in r.anchors:
            raise r.err(f"unknown anchor *{name}")
        return copy.deepcopy(r.anchors[name])
    if c == "" or c in "#]}":
        raise r.err("expected a value")
    return _classify_bare(r, _read_bare(r))


def _read_key(r: _Reader) -> tuple[str, bool]:
    r.skip_trivia()
    if r.peek() == '"':
        return _read_quoted(r), True
    start = r.i
    while not r.eof() and r.peek() in _BARE_KEY:
        r.next()
    if r.i == start:
        raise r.err("expected a key")
    return r.s[start : r.i], False


def _assign(current: dict, key: str, quoted: bool, value: Any, r: _Reader) -> None:
    # Duplicate keys are rejected (matches the reference engine) — a typo
    # duplicated key fails loudly instead of silently winning. Section
    # re-entry (`[a]` twice) still merges: navigation, not assignment.
    if quoted or "." not in key:
        if key in current:
            raise r.err(f"duplicate key {key!r}")
        current[key] = value
        return
    parts = key.split(".")
    node = current
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = node[part] = {}
        if not isinstance(child, dict):
            raise r.err(f"{part!r} redefines a value as a table")
        node = child
    last = parts[-1]
    if last in node:
        raise r.err(f"duplicate key {last!r}")
    node[last] = value


def from_nish(text: str) -> dict:
    """Parse ``NISH/1.0`` core text into plain dicts/lists/scalars.

    Sections (``[a.b]``), arrays (``[[k]]``), dotted keys, comments, ext
    tags (``{"$tag", "value"}``), and anchors/aliases are supported;
    timestamps parse to ``datetime`` (kept as strings when unparseable),
    ``bytes:`` to ``bytes``. Array headers are absolute: every ``[[k]]``
    appends to the root array ``k``, wherever it appears. Malformed input
    raises ``ValueError`` naming the line — ``Request.nish()`` turns that
    into a 400.
    """
    r = _Reader(text.lstrip("\ufeff"))
    r.skip_trivia()
    if r.s.startswith("NISH/", r.i):
        while not r.eof() and r.peek() != "\n":
            r.next()
    root: dict[str, Any] = {}
    stack = [root]  # map context path; top is the current map
    while True:
        r.skip_trivia()
        if r.eof():
            return root
        if r.peek() == "[":
            r.next()
            if r.peek() == "[":
                r.next()
                key, quoted = _read_key(r)
                if quoted or "." in key:
                    raise r.err("array headers take one bare name: [[items]]")
                r.skip_trivia()
                r.expect("]")
                r.expect("]")
                # Array headers are absolute (root scope): repeats append
                # siblings to the same array, wherever they appear.
                slot = root.get(key)
                if slot is None:
                    slot = root[key] = []
                if not isinstance(slot, list):
                    raise r.err(f"{key!r} redefines a table/value as an array")
                item: dict[str, Any] = {}
                slot.append(item)
                stack[-1] = item
            else:
                start = r.i
                while not r.eof() and r.peek() != "]" and r.peek() != "\n":
                    r.next()
                path = r.s[start : r.i].strip()
                r.expect("]")
                if not path or any(not p or any(c not in _BARE_KEY for c in p) for p in path.split(".")):
                    raise r.err(f"bad section [{path}]")
                node = root
                stack = [root]
                for part in path.split("."):
                    child = node.get(part)
                    if child is None:
                        child = node[part] = {}
                    if not isinstance(child, dict):
                        raise r.err(f"{part!r} redefines a value as a table")
                    node = child
                    stack.append(node)
        else:
            key, quoted = _read_key(r)
            r.skip_trivia()
            r.expect("=")
            _assign(stack[-1], key, quoted, _read_value(r), r)
