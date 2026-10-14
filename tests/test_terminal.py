"""ikarem terminal: snapshot/render/keys without a TTY; TTY refused plainly."""

import io
import logging

import pytest

from ikarem import Ikarem
from ikarem import terminal as T


def _app():
    app = Ikarem(enable_docs=False)

    @app.get("/items")
    async def items(req):
        return {"ok": True}

    @app.post("/items")
    async def create(req):
        return {"ok": True}

    return app


def test_snapshot_keys_and_never_raises():
    snap = T.snapshot(_app())
    assert set(snap) >= {"at", "routes", "check", "audit", "metrics", "queue", "logs"}
    assert ("GET", "/items") in snap["routes"]
    assert snap["audit"]["verdict"] in ("clean", "attention", "exposed")
    assert snap["metrics"]["requests"] >= 0


def test_render_pins_content_and_width():
    snap = T.snapshot(_app())
    out = T.render(snap, width=80, color=False)
    assert "/items" in out and "keys:" in out
    assert all(len(line) <= 100 for line in out.splitlines())


def test_render_verdict_states():
    base = T.snapshot(_app())
    assert "ATTENTION" in T.render(base, color=False)  # open POST: honest warning
    clean = dict(base, audit={"verdict": "clean", "pass": 6, "warn": 0, "fail": 0, "bad": []})
    assert "CLEAN" in T.render(clean, color=False)
    bad = dict(base, audit={"verdict": "exposed", "pass": 0, "warn": 0, "fail": 1, "bad": []})
    assert "EXPOSED" in T.render(bad, color=False)
    assert "\x1b" not in T.render(base, color=False)  # plain means plain: zero escapes


def test_key_stream_drives_and_quits():
    stats = T.run(_app(), keys=["r", "tab", "j", "k", "c", "a", "p", "?"])
    assert stats["refreshes"] == 1
    assert "refresh" in stats["actions"] and "check" in stats["actions"] and "audit" in stats["actions"]
    assert any(a.startswith("pentest:") for a in stats["actions"])
    assert set(stats["final"]) >= {"routes", "audit"}


def test_quit_key_stops_immediately():
    stats = T.run(_app(), keys=["q", "r"])
    assert stats["refreshes"] == 0 and stats["actions"] == []


def test_log_tail_captures():
    tail = T.LogTail(keep=5)
    handler = logging.getLogger("ikarem_terminal_test")
    handler.addHandler(tail)
    try:
        handler.warning("hello-%s", "tail")
    finally:
        handler.removeHandler(tail)
    assert any("hello-tail" in line for line in tail.lines)


def test_no_tty_refused_with_fix(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(RuntimeError, match="TTY"):
        T.run(_app(), keys=None)


def test_out_receives_nothing_headless():
    buf = io.StringIO()
    T.run(_app(), keys=["q"], out=buf)
    assert buf.getvalue() == ""
