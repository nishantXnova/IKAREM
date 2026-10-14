"""`ikarem terminal`: fullscreen app dashboard, stdlib only, zero deps.

Live routes, check findings, audit grades, metrics, queue depth, and a
log tail — keyboard-driven, refreshing every 2s. No curses (not stdlib
on Windows), no Textual (not lightweight): hand-rolled ANSI, alternate
screen, msvcrt on Windows and termios elsewhere.

Structure keeps it testable without a TTY: `snapshot()` gathers pure
data, `render()` returns one ANSI string, `run()` drives keys. Tests
inject fake key streams; the live reader is only constructed for real
terminals. Refuses pipes with the fix (`ikarem check` is the headless
answer).
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

TICK = 2.0
PANELS = ("routes", "check", "audit", "metrics")

_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_BLUE = "\x1b[34m"
_REV = "\x1b[7m"
_OFF = "\x1b[0m"
_ALT_ON = "\x1b[?1049h\x1b[H"
_ALT_OFF = "\x1b[?1049l"
_CLEAR = "\x1b[H\x1b[J"


class LogTail(logging.Handler):
    """Ring buffer of formatted log lines for the log strip."""

    def __init__(self, keep: int = 30):
        super().__init__()
        self.lines: list[str] = []
        self.keep = keep
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: Any) -> None:
        try:
            self.lines.append(self.format(record)[-160:])
        except Exception:
            pass
        del self.lines[: -self.keep]


def snapshot(app: Any, tail: LogTail | None = None) -> dict:
    """Gather everything the dashboard shows. Never raises: a dead panel
    reports its error instead of killing the screen."""
    snap: dict[str, Any] = {"at": time.strftime("%H:%M:%S")}
    try:
        from .compiled import describe_app

        manifest = describe_app(app)
        snap["routes"] = [(r["method"], r["path"]) for r in manifest.get("routes", [])]
    except Exception as e:  # noqa: BLE001
        snap["routes"] = []
        snap["routes_error"] = str(e)[:120]
    try:
        report = app.check() if hasattr(app, "check") else None
        if report is None:
            from .compiled import check_app

            report = check_app(app)
        snap["check"] = {
            "errors": list(report.get("errors", [])),
            "warnings": list(report.get("warnings", [])),
        }
    except Exception as e:  # noqa: BLE001
        snap["check"] = {"errors": [f"check crashed: {e}"], "warnings": []}
    try:
        from .security_audit import audit_report

        rep = audit_report(app)
        s = rep["summary"]
        snap["audit"] = {
            "verdict": s["verdict"],
            "pass": s["pass"],
            "warn": s["warn"],
            "fail": s["fail"],
            "bad": [c for c in rep["controls"] if c["status"] != "pass"],
        }
    except Exception as e:  # noqa: BLE001
        snap["audit"] = {
            "verdict": "unknown",
            "pass": 0,
            "warn": 0,
            "fail": 0,
            "bad": [],
            "error": str(e)[:120],
        }
    try:
        from .observability import _METRICS

        n = max(1, _METRICS.get("requests", 0))
        snap["metrics"] = {
            "requests": _METRICS.get("requests", 0),
            "errors": _METRICS.get("errors", 0),
            "avg_ms": _METRICS.get("latency_sum", 0.0) / n * 1000,
            "max_ms": _METRICS.get("latency_max", 0.0) * 1000,
        }
    except Exception:  # noqa: BLE001
        snap["metrics"] = {"requests": 0, "errors": 0, "avg_ms": 0.0, "max_ms": 0.0}
    snap["queue"] = None
    try:
        q = getattr(app, "state_queue", None)
        if q is not None and hasattr(q, "depth"):
            import asyncio as _aio

            try:
                loop = _aio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                snap["queue"] = "live (see worker)"
            else:
                snap["queue"] = _aio.new_event_loop().run_until_complete(q.depth())
    except Exception:  # noqa: BLE001
        snap["queue"] = None
    snap["logs"] = list(tail.lines[-8:]) if tail is not None else []
    return snap


def _glyphs() -> dict:
    """Box glyphs the console can actually encode (cp1252 has no ─/●)."""
    try:
        "─●".encode(getattr(sys.stdout, "encoding", "utf-8") or "utf-8")
        return {"rule": "─", "dot": "●"}
    except (LookupError, UnicodeEncodeError):
        return {"rule": "-", "dot": "o"}


def _verdict_dot(snap: dict, color: bool) -> str:
    g = _glyphs()["dot"]
    a = snap.get("audit", {})
    fails = a.get("fail", 0)
    warns = a.get("warn", 0)
    errs = len(snap.get("check", {}).get("errors", []))
    if fails or errs:
        return f"{_RED}{g}{_OFF} EXPOSED" if color else f"{g} EXPOSED"
    if warns:
        return f"{_YELLOW}{g}{_OFF} ATTENTION" if color else f"{g} ATTENTION"
    return f"{_GREEN}{g}{_OFF} CLEAN" if color else f"{g} CLEAN"


def render(
    snap: dict, selected: int = 0, offsets: dict | None = None, width: int = 100, color: bool = True
) -> str:
    """One full screen. Pure: same inputs → same string (tests pin this).
    `color=False` emits zero escape codes (safe for pipes and cp1252)."""

    def st(code: str) -> str:
        return code if color else ""

    offsets = offsets or {}
    col_w = max(30, (width - 5) // 2)

    def box(title: str, lines: list[str], key: str, height: int = 12) -> list[str]:
        sel = PANELS.index(key) == selected % len(PANELS)
        bar = f"{st(_REV)}{title}{st(_OFF)}" if sel else (f"[{title}]" if sel else title)
        out = [bar]
        start = offsets.get(key, 0)
        for line in lines[start : start + height]:
            out.append(line[:col_w].ljust(col_w))
        while len(out) <= height:
            out.append(" " * col_w)
        return out

    routes = [f"{m:6} {p}" for m, p in snap.get("routes", [])] or [
        "(no routes)" if not snap.get("routes_error") else f"! {snap['routes_error']}"
    ]
    chk = snap.get("check", {})
    check_lines = [f"{st(_RED)}E {e}{st(_OFF)}" for e in chk.get("errors", [])]
    check_lines += [f"{st(_YELLOW)}W {w}{st(_OFF)}" for w in chk.get("warnings", [])]
    check_lines = check_lines or ["check OK"]
    a = snap.get("audit", {})
    audit_lines = [f"{a.get('pass', 0)} pass / {a.get('warn', 0)} warn / {a.get('fail', 0)} fail"]
    audit_lines += [f"{c['id']} {c['name']}" for c in a.get("bad", [])[:8]]
    m = snap.get("metrics", {})
    q = snap.get("queue")
    metric_lines = [
        f"requests {m.get('requests', 0)}   errors {m.get('errors', 0)}",
        f"latency avg {m.get('avg_ms', 0.0):.1f}ms  max {m.get('max_ms', 0.0):.1f}ms",
        f"queue depth {q if q is not None else '— (no QueuePlugin)'}",
    ]
    left = box(f"ROUTES ({len(snap.get('routes', []))})", routes, "routes") + box(
        f"CHECK {len(chk.get('errors', []))}E/{len(chk.get('warnings', []))}W", check_lines, "check"
    )
    right = box(f"AUDIT {a.get('verdict', '?').upper()}", audit_lines, "audit", height=8) + box(
        "METRICS", metric_lines, "metrics", height=8
    )
    head = f"{st(_BOLD)}ikarem terminal{st(_OFF)}  {_verdict_dot(snap, color)}  {st(_DIM)}{snap.get('at', '')}{st(_OFF)}"
    rule = _glyphs()["rule"] * width
    lines = [head, rule]
    limit = width if not color else width + 24  # ANSI codes don't occupy columns
    for i in range(max(len(left), len(right))):
        lcol = left[i] if i < len(left) else " " * col_w
        rcol = right[i] if i < len(right) else ""
        lines.append(f"{lcol} | {rcol}"[:limit])
    lines.append(rule)
    for log in snap.get("logs", [])[-3:]:
        lines.append(f"{st(_DIM)}{log[:width]}{st(_OFF)}")
    lines.append(
        f"{st(_BOLD)}keys:{st(_OFF)} tab panels | up/down/jk scroll | r refresh | c check | a audit | p pentest | ? help | q quit"
    )
    return "\n".join(lines)


def _live_keys():
    """Yield key names, blocking ~50ms between polls. Windows/Unix."""
    if sys.platform == "win32":
        import msvcrt

        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    continue
                yield {"\r": "enter", "\x1b": "esc", "\t": "tab"}.get(ch, ch.lower())
            else:
                import time as _t

                _t.sleep(0.05)
                yield None
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not r:
                    yield None
                    continue
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    seq = ch
                    while select.select([sys.stdin], [], [], 0.02)[0]:
                        seq += sys.stdin.read(1)
                    yield {"\x1b[A": "up", "\x1b[B": "down", "\x1b": "esc"}.get(seq, "esc")
                else:
                    yield {"\r": "enter", "\t": "tab"}.get(ch, ch.lower())
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def run(app: Any, keys: Any = None, out: Any = None, width: int = 100) -> dict:
    """Run the dashboard. `keys`: iterable of key names (tests) or None
    for the live keyboard. Returns {"refreshes": n, "actions": [...]}."""
    out = out or sys.stdout
    tail = LogTail()
    root = logging.getLogger()
    root.addHandler(tail)
    color = hasattr(out, "isatty") and out.isatty() and not __import__("os").environ.get("NO_COLOR")
    if keys is None and not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        raise RuntimeError(
            "ikarem terminal needs a TTY (not a pipe): use `ikarem check`/`ikarem audit` headlessly"
        )
    live = keys is None
    if live:
        out.write(_ALT_ON)
    selected = 0
    offsets: dict[str, int] = {}
    stats = {"refreshes": 0, "actions": []}
    show_help = False
    snap = snapshot(app, tail)
    last_tick = time.time()
    try:
        stream = _live_keys() if live else iter(keys or [])
        for key in stream:
            if key is None:
                if live and time.time() - last_tick < TICK:
                    continue
                last_tick = time.time()
                snap = snapshot(app, tail)
                stats["refreshes"] += 1
            elif key in ("q", "esc"):
                break
            elif key == "r":
                snap = snapshot(app, tail)
                stats["refreshes"] += 1
                stats["actions"].append("refresh")
            elif key == "tab":
                selected = (selected + 1) % len(PANELS)
            elif key in ("j", "down"):
                panel = PANELS[selected % len(PANELS)]
                offsets[panel] = offsets.get(panel, 0) + 1
            elif key in ("k", "up"):
                panel = PANELS[selected % len(PANELS)]
                offsets[panel] = max(0, offsets.get(panel, 0) - 1)
            elif key == "c":
                snap = snapshot(app, tail)
                stats["actions"].append("check")
            elif key == "a":
                snap = snapshot(app, tail)
                stats["actions"].append("audit")
            elif key == "p":
                try:
                    import ikarem_pentest  # noqa: F401

                    stats["actions"].append("pentest:available (run `ikarem-pentest` for the full scan)")
                except ImportError:
                    stats["actions"].append("pentest: pip install ./extensions/ikarem-pentest")
                snap = snapshot(app, tail)
            elif key == "?":
                show_help = not show_help
            if live:
                frame = _help_frame(width) if show_help else render(snap, selected, offsets, width, color)
                out.write(_CLEAR + frame)
                try:
                    out.flush()
                except Exception:
                    pass
    finally:
        if live:
            out.write(_ALT_OFF)
            try:
                out.flush()
            except Exception:
                pass
        root.removeHandler(tail)
    stats["final"] = snap
    return stats


def _help_frame(width: int) -> str:
    return "\n".join(
        [
            f"{_BOLD}ikarem terminal — keys{_OFF}",
            "",
            "  tab      cycle panels (routes/check/audit/metrics)",
            "  ↑↓ j/k   scroll the selected panel",
            "  r        refresh now (auto-refreshes every 2s)",
            "  c / a    re-run check / audit grades now",
            "  p        pentest hint (full scan: `ikarem-pentest`)",
            "  ?        toggle this help",
            "  q / esc  quit (restores your terminal)",
            "",
            "Headless? `ikarem check` / `ikarem audit` print the same data.",
        ]
    )[: width * 12]
