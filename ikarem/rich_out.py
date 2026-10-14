"""Pretty terminal output on Rich, plain text without it.

Importing this module never requires Rich — the import happens lazily
inside `rich_available()`, and every helper falls back to deterministic
plain output otherwise. No-TTY, `NO_COLOR`, and `--no-pretty` all force
the fallback, so piped output (`| jq`, CI logs) stays machine-stable.

Rule: plain output must be byte-identical with or without Rich
installed. Tests pin the fallback strings; the Rich branch is
eyeball-verified, not snapshot-tested (escape codes are brittle).
"""

from __future__ import annotations

import os
import sys
from typing import Any

_RICH: Any = None
_RICH_TRIED = False


def rich_available() -> bool:
    """True when `rich` imports. Cached; never raises."""
    global _RICH, _RICH_TRIED
    if not _RICH_TRIED:
        _RICH_TRIED = True
        try:
            import rich as _mod

            _RICH = _mod
        except ImportError:
            _RICH = None
    return _RICH is not None


def wants_pretty(force: str | None = None, stream: Any = None) -> bool:
    """Decide pretty vs plain. `force`: None (auto), "always", "never"."""
    if force == "never" or os.environ.get("NO_COLOR"):
        return False
    if force == "always":
        return rich_available()
    if not rich_available():
        return False
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def _plain_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return "\n".join(out)


def render_table(title: str, headers: list[str], rows: list[list[str]], pretty: bool = False) -> str:
    """Render a table. Pretty needs a terminal; returns plain string either
    way (callers print it — keeps output capturable and testable)."""
    if pretty and rich_available():
        from io import StringIO

        from rich import box as _box
        from rich.console import Console
        from rich.table import Table

        try:
            "─".encode(getattr(sys.stdout, "encoding", "utf-8") or "utf-8")
            box = None  # Rich default (Unicode lines)
        except (LookupError, UnicodeEncodeError):
            box = _box.ASCII  # cp1252 consoles: +--+ instead of a crash
        table = Table(title=title or None, show_header=True, header_style="bold", box=box)
        for h in headers:
            table.add_column(h)
        for r in rows:
            table.add_row(*r)
        buf = StringIO()
        Console(file=buf, width=100).print(table)
        return buf.getvalue().rstrip("\n")
    lines = [f"{title} ({len(rows)})"] if title else []
    lines.append(_plain_table(headers, rows) if rows else "(none)")
    return "\n".join(lines)


_STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red bold", "info": "cyan"}


def styled(status: str, text: str, pretty: bool = False) -> str:
    """`WARN: detail` plain, or Rich-markup colored when pretty. Pretty
    strings must go through `say()` (Console renders markup); plain
    `print()` would show literal brackets."""
    if pretty and rich_available():
        return f"[{_STATUS_STYLE.get(status, 'white')}]{text}[/]"
    return text


def say(text: str, pretty: bool = False) -> None:
    """Print, rendering Rich markup when pretty, plain otherwise."""
    if pretty and rich_available():
        from rich.console import Console

        Console().print(text)
    else:
        print(text)


class status:
    """Spinner context: Rich status spinner when pretty, one plain line
    otherwise. Never required — absence degrades to print, not failure."""

    def __init__(self, message: str, pretty: bool = False):
        self.message = message
        self.pretty = pretty and rich_available()
        self._ctx: Any = None

    def __enter__(self) -> "status":
        if self.pretty:
            from rich.console import Console

            self._ctx = Console().status(self.message)
            self._ctx.__enter__()
        else:
            print(f"{self.message}…")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._ctx is not None:
            self._ctx.__exit__(*exc)
        return None
