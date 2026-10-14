"""Pretty output: plain fallback pinned, Rich branch only with Rich installed."""

import pytest

from ikarem import rich_out


@pytest.fixture()
def no_rich(monkeypatch):
    monkeypatch.setattr(rich_out, "_RICH", None)
    monkeypatch.setattr(rich_out, "_RICH_TRIED", True)
    monkeypatch.delenv("NO_COLOR", raising=False)


def test_fallback_is_plain_and_stable(no_rich):
    assert rich_out.rich_available() is False
    assert rich_out.wants_pretty() is False
    assert rich_out.wants_pretty("always") is False
    assert rich_out.styled("warn", "WARN: x", pretty=True) == "WARN: x"
    assert (
        rich_out.render_table("t", ["a", "bb"], [["1", "22"], ["333", "4"]], pretty=True)
        == "t (2)\na    bb\n1    22\n333  4"
    )
    assert rich_out.render_table("t", ["a"], [], pretty=True) == "t (0)\n(none)"


def test_no_color_and_never_force_plain(no_rich, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert rich_out.wants_pretty("always") is False
    assert rich_out.wants_pretty("never") is False


def test_non_tty_stream_stays_plain(no_rich):
    class _Pipe:
        def isatty(self):
            return False

        def write(self, s):
            pass

    assert rich_out.wants_pretty(stream=_Pipe()) is False


def test_status_fallback_prints(capsys, no_rich):
    with rich_out.status("working"):
        pass
    assert capsys.readouterr().out == "working…\n"


rich = pytest.importorskip("rich", reason="pip install ikarem[rich] for pretty-output tests")


def test_pretty_table_renders_rows():
    out = rich_out.render_table("t", ["a"], [["1"]], pretty=True)
    assert "a" in out and "1" in out  # Unicode or ASCII box per console encoding
    assert rich_out.styled("fail", "ERROR: x", pretty=True) == "[red bold]ERROR: x[/]"


def test_pretty_mode_flags(monkeypatch, capsys):
    import sys

    from ikarem import Ikarem
    from ikarem.cli import main as cli_main

    app = Ikarem(enable_docs=False)

    @app.get("/p")
    async def p(req):
        return {"ok": True}

    monkeypatch.setattr(sys.modules["ikarem.cli"], "_load", lambda _: app)
    monkeypatch.setattr("sys.argv", ["ikarem", "check", "x:app", "--no-pretty"])
    with pytest.raises(SystemExit) as e:
        cli_main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "checked 1 route(s)" in out and "GET /p -> p" in out
